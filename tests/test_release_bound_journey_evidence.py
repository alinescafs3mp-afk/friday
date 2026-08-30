"""Closed S6-R3 inventories and deterministic release-evidence bundles."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import venv
from dataclasses import replace
from pathlib import Path

import pytest

from tools import exact_release_evidence as evidence
from tools import quality_gate

ROOT = Path(__file__).resolve().parents[1]
CLEAN_CLASS = "clean artifact path"

EXPECTED_CLEAN_INVENTORIES = {
    "conversation_recall": (
        "tests/test_message_window_runtime_integration.py::"
        "test_promoted_exact_window_is_deterministic_scoped_and_receipted[2-complete]",
        "tests/test_message_window_runtime_integration.py::"
        "test_promoted_exact_window_is_deterministic_scoped_and_receipted[21-partial]",
        "tests/test_message_window_runtime_integration.py::"
        "test_promoted_exact_window_is_deterministic_scoped_and_receipted[0-empty]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[content]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[snapshot]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[insert]",
        "tests/test_archive_search_runtime_publication.py::"
        "test_real_router_preserves_two_exact_archive_pages_through_final_answer",
    ),
    "document_recall_answer": (
        "tests/test_v12_file_evidence_reader.py::"
        "test_current_turn_native_files_form_one_process_owned_bundle",
        "tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections",
        "tests/test_archive_search_runtime_publication.py::"
        "test_natural_selected_document_question_uses_bound_preingestion_v12_without_ordinary_paths",
    ),
    "durable_scheduled_work": (
        "tests/test_a_reminder_is_set_before_the_model_speaks.py::"
        "test_the_reminder_is_set_without_asking_the_model",
        "tests/test_durable_scheduled_work_recovery.py::test_two_workers_only_one_claims_pending_task",
        "tests/test_durable_scheduled_work_recovery.py::"
        "test_post_checkpoint_failure_is_uncertain_and_never_replayed[exception]",
        "tests/test_durable_scheduled_work_recovery.py::"
        "test_post_checkpoint_failure_is_uncertain_and_never_replayed[cancelled]",
        "tests/test_reminder_send_edge_storage.py::test_two_storage_workers_get_one_due_reminder_body",
        "tests/test_reminder_send_edge_storage.py::"
        "test_pending_reminder_cannot_be_settled_without_send_edge_claim[sent]",
        "tests/test_reminder_send_edge_storage.py::"
        "test_pending_reminder_cannot_be_settled_without_send_edge_claim[failed]",
        "tests/test_reminder_send_edge_storage.py::"
        "test_pending_reminder_cannot_be_settled_without_send_edge_claim[uncertain]",
        "tests/test_reminder_delivery_fence.py::test_lost_ack_reacks_off_page_after_restart_without_resend",
        "tests/test_release_bound_reminder_scan.py::"
        "test_release_evidence_scan_stops_at_exact_ten_pages_of_two_hundred",
        "tests/test_release_bound_reminder_scan.py::"
        "test_release_evidence_scan_stops_when_continuation_cursor_is_missing",
    ),
    "honest_degradation": (
        "tests/test_search_provider_refusal_is_not_emptiness.py::"
        "test_202_from_duckduckgo_is_a_refusal_not_an_empty_result[asyncio]",
        "tests/test_search_provider_refusal_is_not_emptiness.py::"
        "test_a_provider_that_honestly_found_nothing_is_not_a_refusal[asyncio]",
        "tests/test_search_provider_refusal_is_not_emptiness.py::"
        "test_the_chain_moves_on_when_the_first_provider_refuses[asyncio]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[content]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[snapshot]",
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried[insert]",
        "tests/test_message_window_work_item_runtime.py::"
        "test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
    ),
}


def _identity() -> evidence.ReleaseIdentity:
    return evidence.ReleaseIdentity(
        source_commit="a" * 40,
        tree_sha256="b" * 64,
        wheel_sha256="c" * 64,
        database_schema=50,
    )


def _release_runtime(
    root: Path,
    identity: evidence.ReleaseIdentity,
) -> evidence._AuthenticatedReleaseRuntime:  # noqa: SLF001 - process authority under test
    site = root / "venv/lib/python3.14/site-packages"
    return evidence._AuthenticatedReleaseRuntime(  # noqa: SLF001 - process authority under test
        root=root,
        identity=identity,
        interpreter=root / "venv/bin/python",
        interpreter_ref="venv/bin/python",
        site_packages=site,
        site_packages_ref="venv/lib/python3.14/site-packages",
        package_root=site / "friday",
        authority=evidence._RELEASE_RUNTIME_AUTHORITY,  # noqa: SLF001
    )


def _receipt(
    identity: evidence.ReleaseIdentity,
    journey_id: str,
    *,
    result: str = "VERIFIED",
) -> bytes:
    return evidence.canonical_json_bytes(
        {
            "$schema": evidence.CLEAN_ARTIFACT_RECEIPT_SCHEMA,
            "check_ids": evidence._check_ids(journey_id, CLEAN_CLASS),  # noqa: SLF001
            "environment": "clean_artifact",
            "evidence_class": CLEAN_CLASS,
            "execution": {
                "artifact_import": {
                    "interpreter_ref": "venv/bin/python",
                    "origin_report_sha256": "1" * 64,
                    "site_packages_ref": "venv/lib/python3.14/site-packages",
                    "subprocess_policy": evidence._SUBPROCESS_POLICY,  # noqa: SLF001
                    "tooling_modules_sha256": "2" * 64,
                    "tooling_policy": evidence._TEST_TOOLING_POLICY,  # noqa: SLF001
                    "tooling_snapshot_sha256": "3" * 64,
                },
                "collection_sha256": "d" * 64,
                "exit_code": 0 if result == "VERIFIED" else 1,
                "outcome_projection_sha256": "e" * 64,
                "producer_path": evidence.PRODUCER_PATH,
                "producer_source_sha256": "f" * 64,
                "runner": "pytest",
            },
            "journey_id": journey_id,
            "observed_at_utc": "2026-08-30T08:00:00Z",
            "owner_smoke": None,
            "proofs": [],
            "release": identity.payload(),
            "result": result,
        }
    )


def _private_bundle_path(root: Path, ref: str) -> Path:
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    current = root
    for part in Path(ref).parts[:-1]:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)
    return root / ref


def test_clean_artifact_inventories_are_exact_closed_and_executable() -> None:
    observed_paths: set[str] = set()
    for journey_id, expected in EXPECTED_CLEAN_INVENTORIES.items():
        refs = evidence.proof_refs(journey_id, CLEAN_CLASS)
        assert refs == expected
        assert len(refs) == len(set(refs))
        assert set(refs).isdisjoint(evidence._GENERIC_OPERATOR_REFS)  # noqa: SLF001
        for ref in refs:
            path_text, locator = ref.split("::")
            function_name = locator.partition("[")[0]
            source = ROOT / path_text
            module = ast.parse(source.read_text(encoding="utf-8"), filename=path_text)
            top_level = {
                node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert function_name in top_level
            observed_paths.add(path_text)
    assert tuple(EXPECTED_CLEAN_INVENTORIES) == (
        "conversation_recall",
        "document_recall_answer",
        "durable_scheduled_work",
        "honest_degradation",
    )
    assert len(observed_paths) == 10
    for unsupported in ("obsidian_write_sync", "current_file_web_comparison"):
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^proof_inventory_invalid$"):
            evidence.proof_refs(unsupported, CLEAN_CLASS)


def test_clean_artifact_schema_and_check_ids_are_truthful_and_narrow() -> None:
    assert evidence.receipt_schema(CLEAN_CLASS) == evidence.CLEAN_ARTIFACT_RECEIPT_SCHEMA
    assert evidence.receipt_schema("integration path") == evidence.RECEIPT_SCHEMA
    assert evidence.CLEAN_ARTIFACT_RECEIPT_SCHEMA not in {
        evidence.RECEIPT_SCHEMA,
        "friday.golden-journey-sanitized-receipt.v3",
    }
    for journey_id in EXPECTED_CLEAN_INVENTORIES:
        assert evidence._check_ids(journey_id, CLEAN_CLASS) == [  # noqa: SLF001
            f"{journey_id}.installed_journey_suite"
        ]


def test_clean_receipt_validation_requires_the_authenticated_sealed_release(tmp_path: Path) -> None:
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_required$"):
        evidence.validate_receipt(
            _receipt(_identity(), "conversation_recall"),
            expected_release=_identity(),
            expected_journey_id="conversation_recall",
            expected_evidence_class=CLEAN_CLASS,
            repo_root=tmp_path,
        )


def test_clean_receipt_schema_origin_and_runtime_authority_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    runtime = _release_runtime(tmp_path / "release", identity)
    test_ref = "tests/test_probe.py::test_probe"
    source = b"def test_probe():\n    pass\n"
    refs = (test_ref,)
    collection = evidence.canonical_json_bytes({"nodeids": list(refs), "version": 1})
    witness = evidence._execution_witness(  # noqa: SLF001 - exact internal witness
        ("PASSED",),
        0,
        hashlib.sha256(collection).hexdigest(),
        evidence._outcome_projection_sha256(refs, ("PASSED",)),  # noqa: SLF001
        artifact_origin_sha256="1" * 64,
        interpreter_ref=runtime.interpreter_ref,
        site_packages_ref=runtime.site_packages_ref,
        subprocess_policy=evidence._SUBPROCESS_POLICY,  # noqa: SLF001
        tooling_modules_sha256="2" * 64,
        tooling_policy=evidence._TEST_TOOLING_POLICY,  # noqa: SLF001
        tooling_snapshot_sha256="3" * 64,
    )
    monkeypatch.setattr(evidence, "proof_refs", lambda *_args: refs)
    monkeypatch.setattr(
        evidence,
        "_check_ids",
        lambda *_args: ["conversation_recall.installed_journey_suite"],
    )
    monkeypatch.setattr(
        evidence,
        "_source_proofs",
        lambda *_args, **_kwargs: ("f" * 64, []),
    )
    monkeypatch.setattr(evidence, "_test_source", lambda *_args: source)
    payload = json.loads(_receipt(identity, "conversation_recall"))
    payload["proofs"] = [
        {
            "outcome": "PASSED",
            "runner": "pytest",
            "test_ref": test_ref,
            "test_source_sha256": hashlib.sha256(source).hexdigest(),
        }
    ]
    payload["execution"]["collection_sha256"] = witness.collection_sha256
    payload["execution"]["outcome_projection_sha256"] = witness.outcome_projection_sha256
    raw = evidence.canonical_json_bytes(payload)

    assert (
        evidence._validate_receipt(  # noqa: SLF001 - closed structural validation
            raw,
            expected_release=identity,
            expected_journey_id="conversation_recall",
            expected_evidence_class=CLEAN_CLASS,
            repo_root=tmp_path,
            execution_witness=witness,
            release_runtime=runtime,
        )["result"]
        == "VERIFIED"
    )

    downgraded = dict(payload)
    downgraded["$schema"] = evidence.RECEIPT_SCHEMA
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^receipt_binding_invalid$"):
        evidence._validate_receipt(  # noqa: SLF001
            evidence.canonical_json_bytes(downgraded),
            expected_release=identity,
            expected_journey_id="conversation_recall",
            expected_evidence_class=CLEAN_CLASS,
            repo_root=tmp_path,
            execution_witness=witness,
            release_runtime=runtime,
        )

    tampered = json.loads(raw)
    tampered["execution"]["artifact_import"]["origin_report_sha256"] = "2" * 64
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^execution_evidence_mismatch$"):
        evidence._validate_receipt(  # noqa: SLF001
            evidence.canonical_json_bytes(tampered),
            expected_release=identity,
            expected_journey_id="conversation_recall",
            expected_evidence_class=CLEAN_CLASS,
            repo_root=tmp_path,
            execution_witness=witness,
            release_runtime=runtime,
        )

    for field, replacement in (
        ("interpreter_ref", "venv/bin/host-python"),
        ("tooling_policy", "implicit_path"),
        ("tooling_modules_sha256", "3" * 64),
        ("tooling_snapshot_sha256", "4" * 64),
    ):
        tampered = json.loads(raw)
        tampered["execution"]["artifact_import"][field] = replacement
        with pytest.raises(
            evidence.ExactReleaseEvidenceError,
            match="^(artifact_execution_binding_invalid|execution_evidence_mismatch)$",
        ):
            evidence._validate_receipt(  # noqa: SLF001
                evidence.canonical_json_bytes(tampered),
                expected_release=identity,
                expected_journey_id="conversation_recall",
                expected_evidence_class=CLEAN_CLASS,
                repo_root=tmp_path,
                execution_witness=witness,
                release_runtime=runtime,
            )

    forged = replace(runtime, authority=object())
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="^release_runtime_not_authenticated$",
    ):
        evidence._require_release_runtime(forged, identity)  # noqa: SLF001

    oversized_report = tmp_path / "oversized-origin.json"
    oversized_report.write_bytes(b"x" * 4097)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^artifact_origin_report_invalid$"):
        evidence._artifact_origin_report_sha256(oversized_report, runtime)  # noqa: SLF001


def _write_stub_interpreter(root: Path) -> Path:
    interpreter = root / "venv/bin/python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"sealed-python")
    interpreter.chmod(0o500)
    return interpreter


def _write_release_runtime_shape(root: Path) -> Path:
    site = root / "venv/lib/python3.14/site-packages"
    (site / "friday").mkdir(parents=True)
    (site / "friday/__init__.py").write_text("ORIGIN = 'sealed'\n", encoding="ascii")
    (site / "friday-1.0.dist-info").mkdir()
    _write_stub_interpreter(root)
    return site


def test_release_runtime_discovery_is_unique_physical_and_identity_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    root = tmp_path / "release"
    site = _write_release_runtime_shape(root)
    monkeypatch.setattr(evidence, "derive_release_identity", lambda candidate: identity)

    runtime = evidence._authenticate_release_runtime(root)  # noqa: SLF001
    assert runtime.identity == identity
    assert runtime.interpreter == root / "venv/bin/python"
    assert runtime.interpreter_ref == "venv/bin/python"
    assert runtime.site_packages == site
    assert runtime.site_packages_ref == "venv/lib/python3.14/site-packages"

    (site / "friday-2.0.dist-info").mkdir()
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(root)  # noqa: SLF001


def test_release_runtime_rejects_multiple_or_symlinked_installed_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(evidence, "derive_release_identity", lambda candidate: identity)

    multiple = tmp_path / "multiple"
    _write_release_runtime_shape(multiple)
    second = multiple / "venv/lib/python3.13/site-packages"
    (second / "friday").mkdir(parents=True)
    (second / "friday/__init__.py").write_text("", encoding="ascii")
    (second / "friday-1.0.dist-info").mkdir()
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(multiple)  # noqa: SLF001

    linked = tmp_path / "linked"
    site = linked / "venv/lib/python3.14/site-packages"
    site.mkdir(parents=True)
    outside = tmp_path / "outside-friday"
    outside.mkdir()
    (outside / "__init__.py").write_text("", encoding="ascii")
    (site / "friday").symlink_to(outside, target_is_directory=True)
    (site / "friday-1.0.dist-info").mkdir()
    _write_stub_interpreter(linked)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(linked)  # noqa: SLF001

    mismatched_python = tmp_path / "mismatched-python"
    mismatched_site = mismatched_python / "venv/lib/python3.13/site-packages"
    (mismatched_site / "friday").mkdir(parents=True)
    (mismatched_site / "friday/__init__.py").write_text("", encoding="ascii")
    (mismatched_site / "friday-1.0.dist-info").mkdir()
    _write_stub_interpreter(mismatched_python)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(mismatched_python)  # noqa: SLF001

    missing_interpreter = tmp_path / "missing-interpreter"
    _write_release_runtime_shape(missing_interpreter)
    (missing_interpreter / "venv/bin/python").unlink()
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(missing_interpreter)  # noqa: SLF001

    linked_interpreter = tmp_path / "linked-interpreter"
    _write_release_runtime_shape(linked_interpreter)
    interpreter = linked_interpreter / "venv/bin/python"
    interpreter.unlink()
    interpreter.symlink_to(sys.executable)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(linked_interpreter)  # noqa: SLF001

    non_executable = tmp_path / "non-executable"
    _write_release_runtime_shape(non_executable)
    (non_executable / "venv/bin/python").chmod(0o600)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(non_executable)  # noqa: SLF001


def test_test_tooling_is_one_explicit_external_physical_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    release = tmp_path / "release"
    source.mkdir()
    release.mkdir()

    tooling = evidence._test_tooling_site(source, release)  # noqa: SLF001

    assert tooling.is_dir()
    assert tooling.resolve(strict=True) == tooling
    assert not tooling.is_relative_to(source)
    assert not tooling.is_relative_to(release)
    real_find_spec = evidence.importlib.machinery.PathFinder.find_spec
    monkeypatch.setattr(
        evidence.importlib.machinery.PathFinder,
        "find_spec",
        staticmethod(
            lambda name, path=None, target=None: (
                None if name == "pytest" else real_find_spec(name, path, target)
            )
        ),
    )
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^test_tooling_invalid$"):
        evidence._test_tooling_site(source, release)  # noqa: SLF001


def test_private_tooling_snapshot_detects_modify_then_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    release = tmp_path / "release"
    scratch = tmp_path / "scratch"
    source.mkdir()
    release.mkdir()
    scratch.mkdir(mode=0o700)
    tooling_site = evidence._test_tooling_site(source, release)  # noqa: SLF001
    snapshot, projection = evidence._snapshot_test_tooling(tooling_site, scratch)  # noqa: SLF001
    content_digest = evidence._tooling_snapshot_content_sha256(projection)  # noqa: SLF001
    changed_projection = [dict(entry) for entry in projection]
    dist_info = next(
        entry
        for entry in changed_projection
        if entry["kind"] == "file" and ".dist-info/" in str(entry["path"])
    )
    dist_info["sha256"] = "0" * 64 if dist_info["sha256"] != "0" * 64 else "1" * 64
    assert (
        evidence._tooling_snapshot_content_sha256(tuple(changed_projection))  # noqa: SLF001
        != content_digest
    )
    metadata_only = [dict(entry) for entry in projection]
    metadata_only[0]["ctime_ns"] = int(metadata_only[0]["ctime_ns"]) + 1
    assert evidence._tooling_snapshot_content_sha256(tuple(metadata_only)) == content_digest  # noqa: SLF001
    pytest_init = snapshot / "pytest/__init__.py"
    original = pytest_init.read_bytes()

    pytest_init.chmod(0o600)
    pytest_init.write_bytes(original + b"\n# transient executed replacement\n")
    pytest_init.write_bytes(original)
    pytest_init.chmod(0o400)

    try:
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^test_tooling_changed$"):
            evidence._require_tooling_snapshot_unchanged(snapshot, projection)  # noqa: SLF001
        assert projection
    finally:
        for directory in (snapshot, *(path for path in snapshot.rglob("*") if path.is_dir())):
            directory.chmod(0o700)


def _write_bootstrap_fixture(tmp_path: Path, test_body: str) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    release = tmp_path / "release"
    venv.EnvBuilder(with_pip=False, symlinks=False).create(release / "venv")
    site = release / "venv/lib/python3.14/site-packages"
    (source / "tools").mkdir(parents=True)
    (source / "tools/__init__.py").write_text("", encoding="ascii")
    (source / "tools/quality_gate.py").write_text("", encoding="ascii")
    (source / "friday").mkdir()
    (source / "friday/__init__.py").write_text(
        "raise RuntimeError('source checkout package imported')\n",
        encoding="ascii",
    )
    (site / "friday").mkdir(parents=True)
    (site / "friday/__init__.py").write_text("ORIGIN = 'sealed'\n", encoding="ascii")
    probe = source / "test_probe.py"
    probe.write_text(test_body, encoding="ascii")
    return source, release, site, probe


def _run_installed_bootstrap(
    source: Path,
    release: Path,
    site: Path,
    probe: Path,
    report: Path,
    *,
    interpreter: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    source_tooling_site = evidence._test_tooling_site(source, release)  # noqa: SLF001
    runtime = _release_runtime(release, _identity())
    with (
        tempfile.TemporaryDirectory(prefix="sealed-bootstrap-", dir=report.parent) as temporary,
        quality_gate._isolated_test_environment() as base_environment,  # noqa: SLF001
    ):
        scratch = Path(temporary).resolve(strict=True)
        internal_report = scratch / "artifact-origin.json"
        tooling_site, tooling_projection = evidence._snapshot_test_tooling(  # noqa: SLF001
            source_tooling_site,
            scratch,
        )
        environment = evidence._sealed_pytest_environment(  # noqa: SLF001
            base_environment,
            runtime,
            scratch,
            scratch / "python-cache",
        )
        completed = subprocess.run(
            (
                str(interpreter or release / "venv/bin/python"),
                "-I",
                "-S",
                "-B",
                "-X",
                f"pycache_prefix={scratch / 'python-cache'}",
                "-c",
                evidence._INSTALLED_PYTEST_BOOTSTRAP,  # noqa: SLF001
                str(source),
                str(release),
                str(site),
                site.relative_to(release).as_posix(),
                "venv/bin/python",
                str(tooling_site),
                hashlib.sha256((source / "tools/quality_gate.py").read_bytes()).hexdigest(),
                str(internal_report),
                "a" * 40,
                "c" * 64,
                "-q",
                "-o",
                "addopts=",
                "-o",
                "pythonpath=",
                "--import-mode=importlib",
                "-p",
                "no:cacheprovider",
                f"--basetemp={scratch / 'pytest'}",
                str(probe),
            ),
            cwd=source,
            check=False,
            capture_output=True,
            env=environment,
            timeout=60,
        )
        evidence._require_tooling_snapshot_unchanged(  # noqa: SLF001
            tooling_site,
            tooling_projection,
        )
        if completed.returncode == 0:
            report.write_bytes(internal_report.read_bytes())
        return completed


def test_installed_bootstrap_imports_first_party_only_from_the_sealed_site(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "from friday import ORIGIN\n\ndef test_origin():\n    assert ORIGIN == 'sealed'\n",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    raw = report.read_bytes()
    payload = json.loads(raw)
    assert raw == evidence.canonical_json_bytes(payload)
    assert payload["schema"] == "friday.clean-artifact-import-origin.v1"
    assert payload["interpreter_ref"] == "venv/bin/python"
    assert payload["site_packages_ref"] == "venv/lib/python3.14/site-packages"
    assert payload["source_commit"] == "a" * 40
    assert payload["wheel_sha256"] == "c" * 64
    assert payload["subprocess_policy"] == evidence._SUBPROCESS_POLICY  # noqa: SLF001
    assert payload["tooling_module_count"] >= len(evidence._TEST_TOOLING_MODULES)  # noqa: SLF001
    assert len(payload["tooling_modules_sha256"]) == 64
    assert payload["tooling_policy"] == evidence._TEST_TOOLING_POLICY  # noqa: SLF001
    assert payload["module_count"] >= 2
    repeated_report = tmp_path / "origin-repeated.json"
    repeated = _run_installed_bootstrap(source, release, site, probe, repeated_report)
    assert repeated.returncode == 0, repeated.stderr.decode(errors="replace")
    assert repeated_report.read_bytes() == raw


def test_installed_bootstrap_rejects_preinserted_first_party_submodule(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import importlib\n\n"
        "preinserted = importlib.import_module('friday.preinserted')\n\n"
        "def test_preinserted_module_was_used():\n"
        "    assert preinserted.VALUE == 'forged'\n",
    )
    (site / "friday/preinserted.py").write_text("VALUE = 'sealed'\n", encoding="ascii")
    (site / "friday/__init__.py").write_text(
        "import importlib.machinery, importlib.util, sys, types\n"
        "from pathlib import Path\n"
        "name = 'friday.preinserted'\n"
        "origin = Path(__file__).with_name('preinserted.py')\n"
        "loader = importlib.machinery.SourceFileLoader(name, str(origin))\n"
        "spec = importlib.util.spec_from_file_location(name, str(origin), loader=loader)\n"
        "fake = types.ModuleType(name)\n"
        "fake.__file__ = str(origin)\n"
        "fake.__loader__ = loader\n"
        "fake.__package__ = 'friday'\n"
        "fake.__spec__ = spec\n"
        "fake.VALUE = 'forged'\n"
        "sys.modules[name] = fake\n"
        "ORIGIN = 'sealed'\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"first_party_module_unattested" in completed.stderr


def test_candidate_pytest_module_poisoning_cannot_forge_verified(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday\n\ndef test_candidate_cannot_forge_success():\n    assert True\n",
    )
    (site / "friday/__init__.py").write_text(
        "import sys, types\n"
        "real_pytest = sys.modules['pytest']\n"
        "fake_pytest = types.ModuleType('pytest')\n"
        "fake_pytest.__file__ = real_pytest.__file__\n"
        "fake_pytest.__loader__ = real_pytest.__loader__\n"
        "fake_pytest.__spec__ = real_pytest.__spec__\n"
        "fake_pytest.main = lambda *_args, **_kwargs: (print('VERIFIED') or 0)\n"
        "sys.modules['pytest'] = fake_pytest\n"
        "ORIGIN = 'sealed'\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"VERIFIED" not in completed.stdout
    assert b"test_tooling_module_poisoned" in completed.stderr


def test_candidate_cannot_preinsert_lazy_tooling_submodule_with_snapshot_origin(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday, importlib\n\n"
        "lazy_forged = importlib.import_module('pytest.lazy_forged')\n\n"
        "def test_forged_lazy_tooling_module_was_used():\n"
        "    assert lazy_forged.VALUE == 'forged'\n",
    )
    (site / "friday/__init__.py").write_text(
        "import sys, types\n"
        "real_pytest = sys.modules['pytest']\n"
        "fake = types.ModuleType('pytest.lazy_forged')\n"
        "fake.__file__ = real_pytest.__file__\n"
        "fake.__loader__ = real_pytest.__loader__\n"
        "fake.__package__ = 'pytest'\n"
        "fake.__spec__ = real_pytest.__spec__\n"
        "fake.VALUE = 'forged'\n"
        "sys.modules['pytest.lazy_forged'] = fake\n"
        "ORIGIN = 'sealed'\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"test_tooling_module_unattested" in completed.stderr


def test_candidate_cannot_persistently_poison_lazy_tooling_callable(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday\n\ndef test_lazy_tooling_callable_stays_authenticated():\n"
        "    assert True\n",
    )
    (site / "friday/__init__.py").write_text(
        "import importlib, sys, types\n"
        "target = None\n"
        "for name in (\n"
        "    'packaging._manylinux', 'packaging._musllinux', 'packaging._elffile',\n"
        "    'packaging.metadata', 'packaging.tags',\n"
        "):\n"
        "    if name not in sys.modules:\n"
        "        lazy = importlib.import_module(name)\n"
        "        target = next(\n"
        "            (value for value in vars(lazy).values() if isinstance(value, types.FunctionType)),\n"
        "            None,\n"
        "        )\n"
        "        if target is not None:\n"
        "            break\n"
        "if target is None:\n"
        "    raise RuntimeError('no lazy tooling module available')\n"
        "def forged(*_args, **_kwargs):\n"
        "    return True\n"
        "target.__code__ = forged.__code__\n"
        "ORIGIN = 'sealed'\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"test_tooling_callable_poisoned" in completed.stderr


def test_candidate_in_place_pytest_callable_poisoning_cannot_forge_verified(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday\n\ndef test_candidate_cannot_forge_success():\n    assert True\n",
    )
    (site / "friday/__init__.py").write_text(
        "import sys\n"
        "def forged_main(*_args, **_kwargs):\n"
        "    print('VERIFIED')\n"
        "    return 0\n"
        "sys.modules['pytest'].main.__code__ = forged_main.__code__\n"
        "ORIGIN = 'sealed'\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"VERIFIED" not in completed.stdout
    assert b"test_tooling_callable_poisoned" in completed.stderr


def test_installed_bootstrap_disables_release_pth_and_sitecustomize_startup(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday, importlib.util\n\ndef test_unbound_path_is_absent():\n"
        "    assert importlib.util.find_spec('unbound_runtime_dependency') is None\n",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "unbound_runtime_dependency.py").write_text("VALUE = 'outside'\n", encoding="ascii")
    marker = tmp_path / "pth-executed"
    (site / "release-startup.pth").write_text(
        f"import pathlib;pathlib.Path({str(marker)!r}).write_text('executed')\n{outside}\n",
        encoding="utf-8",
    )
    (site / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize')\n",
        encoding="utf-8",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert report.is_file()
    assert not marker.exists()


def test_installed_bootstrap_rejects_the_host_interpreter(tmp_path: Path) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "from friday import ORIGIN\n\ndef test_origin():\n    assert ORIGIN == 'sealed'\n",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(
        source,
        release,
        site,
        probe,
        report,
        interpreter=Path(sys.executable),
    )

    assert completed.returncode != 0
    assert not report.exists()
    assert b"installed_site_binding_invalid" in completed.stderr


def test_installed_bootstrap_treats_caught_child_execution_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import subprocess\n\ndef test_child_is_caught():\n"
        "    try:\n        subprocess.run(['true'], check=False)\n"
        "    except RuntimeError:\n        pass\n",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"child_execution_unattested" in completed.stderr


def test_installed_bootstrap_rejects_caught_unchecked_hash_bytecode_seeding(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday\n\ndef test_candidate_cannot_seed_bytecode():\n"
        "    assert friday.LATER == 'forged'\n",
    )
    (site / "friday/later.py").write_text("LATER = 'source'\n", encoding="ascii")
    (site / "friday/__init__.py").write_text(
        "import importlib.util, os, pathlib, py_compile\n"
        "source = pathlib.Path(__file__).with_name('later.py')\n"
        "cache = pathlib.Path(importlib.util.cache_from_source(str(source)))\n"
        "forged = pathlib.Path(os.environ['TMPDIR']) / 'forged-later.py'\n"
        "forged.write_text(\"LATER = 'forged'\\n\", encoding='ascii')\n"
        "cache.parent.mkdir(parents=True, exist_ok=True)\n"
        "try:\n"
        "    py_compile.compile(\n"
        "        str(forged), cfile=str(cache), doraise=True,\n"
        "        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,\n"
        "    )\n"
        "except RuntimeError:\n"
        "    pass\n"
        "from .later import LATER\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"bytecode_execution_unattested" in completed.stderr


def test_installed_bootstrap_excludes_non_test_dependency_from_tooling_snapshot(tmp_path: Path) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday, importlib.util\n\ndef test_tooling_fallback_is_absent():\n"
        "    assert importlib.util.find_spec('coverage') is None\n",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert report.is_file()


def test_installed_bootstrap_treats_caught_source_import_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday, importlib\nfrom pathlib import Path\n\ndef test_escape_is_caught():\n"
        "    friday.__path__ = [str(Path(__file__).parent / 'friday')]\n"
        "    try:\n        importlib.import_module('friday.escaped')\n"
        "    except RuntimeError:\n        pass\n",
    )
    (source / "friday/escaped.py").write_text("SOURCE_BODY = 'private'\n", encoding="ascii")
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"first_party_origin_escaped_release" in completed.stderr


def test_installed_bootstrap_denies_python_alias_reads_from_checkout_product(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import runpy\nfrom pathlib import Path\n\ndef test_alias_read_is_caught():\n"
        "    try:\n        runpy.run_path(Path(__file__).parent / 'friday' / 'escaped.py')\n"
        "    except RuntimeError:\n        pass\n",
    )
    (source / "friday/escaped.py").write_text("SOURCE_BODY = 'private'\n", encoding="ascii")
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"source_first_party_read_unattested" in completed.stderr


def test_installed_bootstrap_denies_dir_fd_alias_reads_from_checkout_product(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import os\nfrom pathlib import Path\n\ndef test_dir_fd_read_is_caught():\n"
        "    root_fd = os.open(Path(__file__).parent, os.O_RDONLY | os.O_DIRECTORY)\n"
        "    try:\n"
        "        try:\n            os.open('friday/escaped.py', os.O_RDONLY, dir_fd=root_fd)\n"
        "        except RuntimeError:\n            pass\n"
        "    finally:\n        os.close(root_fd)\n",
    )
    (source / "friday/escaped.py").write_text("SOURCE_BODY = 'private'\n", encoding="ascii")
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"source_first_party_read_unattested" in completed.stderr


def test_installed_bootstrap_denies_caught_hardlink_alias_of_checkout_product(
    tmp_path: Path,
) -> None:
    source, release, site, probe = _write_bootstrap_fixture(
        tmp_path,
        "import friday\n\ndef test_hardlink_alias_cannot_expose_source():\n"
        "    assert friday.LEAKED is False\n",
    )
    (source / "friday/escaped.py").write_text("SOURCE_BODY = 'private'\n", encoding="ascii")
    (site / "friday/__init__.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "source_fd = os.open(Path.cwd(), os.O_RDONLY | os.O_DIRECTORY)\n"
        "target_fd = os.open(os.environ['TMPDIR'], os.O_RDONLY | os.O_DIRECTORY)\n"
        "LEAKED = False\n"
        "try:\n"
        "    try:\n"
        "        os.link(\n"
        "            'friday/escaped.py', 'source-alias',\n"
        "            src_dir_fd=source_fd, dst_dir_fd=target_fd,\n"
        "        )\n"
        "        alias_fd = os.open('source-alias', os.O_RDONLY, dir_fd=target_fd)\n"
        "        try:\n"
        "            LEAKED = b'private' in os.read(alias_fd, 4096)\n"
        "        finally:\n"
        "            os.close(alias_fd)\n"
        "    except RuntimeError:\n"
        "        pass\n"
        "finally:\n"
        "    os.close(target_fd)\n"
        "    os.close(source_fd)\n",
        encoding="ascii",
    )
    report = tmp_path / "origin.json"

    completed = _run_installed_bootstrap(source, release, site, probe, report)

    assert completed.returncode != 0
    assert not report.exists()
    assert b"source_first_party_alias_unattested" in completed.stderr


def test_closed_clean_runner_binds_the_installed_bootstrap_and_origin_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    runtime = _release_runtime(tmp_path / "release", identity)
    tooling_site = tmp_path / "tooling-site"
    ref = "tests/test_probe.py::test_probe"
    commands: list[tuple[str, ...]] = []
    origin_raw: list[bytes] = []
    monkeypatch.setattr(evidence, "proof_refs", lambda *_args: (ref,))
    monkeypatch.setattr(evidence, "_require_exact_checkout", lambda *_args: None)
    monkeypatch.setattr(
        evidence,
        "_source_proofs",
        lambda *_args, **_kwargs: ("f" * 64, []),
    )
    monkeypatch.setattr(evidence, "_reauthenticate_release_runtime", lambda *_args: None)
    monkeypatch.setattr(evidence, "_authenticated_quality_gate", lambda **_kwargs: quality_gate)
    monkeypatch.setattr(
        quality_gate,
        "__authenticated_source_sha256__",
        "5" * 64,
        raising=False,
    )
    monkeypatch.setattr(evidence, "_test_tooling_site", lambda *_args: tooling_site)
    monkeypatch.setattr(
        evidence,
        "_snapshot_test_tooling",
        lambda *_args: (tooling_site, ()),
    )
    monkeypatch.setattr(evidence, "_require_tooling_snapshot_unchanged", lambda *_args: None)
    monkeypatch.setattr(evidence.sys, "executable", "/producer/python")
    poison = {
        "LD_PRELOAD": "/private/inject.so",
        "LD_AUDIT": "/private/audit.so",
        "LD_LIBRARY_PATH": "/private/lib",
        "DYLD_INSERT_LIBRARIES": "/private/dyld.dylib",
        "GLIBC_TUNABLES": "glibc.malloc.perturb=1",
        "PYTHONINSPECT": "1",
        "PYTHONSTARTUP": "/private/startup.py",
        "SECRET_SENTINEL": "must-not-cross-process-boundary",
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)

    def run_pytest(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        environment = kwargs["env"]
        assert set(environment) == {
            "FRIDAY_HOME",
            "JERICHO_HOME",
            "FRIDAY_ENV_FILE",
            "JERICHO_ENV_FILE",
            "FRIDAY_DATABASE_PATH",
            "JERICHO_DATABASE_PATH",
            "FRIDAY_DATABASE_MUST_EXIST",
            "JERICHO_DATABASE_MUST_EXIST",
            "FRIDAY_LLM_ENABLED",
            "FRIDAY_EMBEDDINGS_ENABLED",
            "FRIDAY_WORKERS_ENABLED",
            "FRIDAY_CODE_EXECUTION_ENABLED",
            "FRIDAY_TEST_BACKUPS_DIR",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONHASHSEED",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTHONPYCACHEPREFIX",
            "TMPDIR",
            "TZ",
            "VIRTUAL_ENV",
        }
        assert not poison.keys() & environment.keys()
        assert environment["HOME"] == environment["FRIDAY_HOME"]
        assert Path(environment["TMPDIR"]).parent == Path(environment["PYTHONPYCACHEPREFIX"]).parent
        assert environment["VIRTUAL_ENV"] == str(runtime.root / "venv")
        assert environment["PATH"] == os.defpath
        assert "PYTHONHOME" not in environment
        assert "PYTHONPATH" not in environment
        report = Path(next(item[11:] for item in command if item.startswith("--junitxml=")))
        collection = Path(
            next(item[29:] for item in command if item.startswith("--friday-collection-manifest="))
        )
        bootstrap_index = command.index(evidence._INSTALLED_PYTEST_BOOTSTRAP)  # noqa: SLF001
        origin = Path(command[bootstrap_index + 8])
        collection.write_bytes(evidence.canonical_json_bytes({"nodeids": [ref], "version": 1}))
        report.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="probe"><properties>'
            f'<property name="friday_nodeid" value="{ref}"/>'
            "</properties></testcase></testsuite>",
            encoding="utf-8",
        )
        raw = evidence.canonical_json_bytes(
            {
                "interpreter_ref": runtime.interpreter_ref,
                "module_count": 2,
                "module_origins_sha256": "3" * 64,
                "schema": "friday.clean-artifact-import-origin.v1",
                "site_packages_ref": runtime.site_packages_ref,
                "source_commit": identity.source_commit,
                "subprocess_policy": evidence._SUBPROCESS_POLICY,  # noqa: SLF001
                "tooling_module_count": 7,
                "tooling_modules_sha256": "4" * 64,
                "tooling_policy": evidence._TEST_TOOLING_POLICY,  # noqa: SLF001
                "wheel_sha256": identity.wheel_sha256,
            }
        )
        origin.write_bytes(raw)
        origin_raw.append(raw)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(evidence.subprocess, "run", run_pytest)

    witness = evidence._run_closed_pytest(  # noqa: SLF001
        tmp_path,
        identity,
        "conversation_recall",
        CLEAN_CLASS,
        release_runtime=runtime,
    )

    command = commands[0]
    assert command[:8] == (
        str(runtime.interpreter),
        "-I",
        "-S",
        "-B",
        "-X",
        command[5],
        "-c",
        evidence._INSTALLED_PYTEST_BOOTSTRAP,  # noqa: SLF001
    )
    bootstrap_index = command.index(evidence._INSTALLED_PYTEST_BOOTSTRAP)  # noqa: SLF001
    assert command[bootstrap_index + 1 : bootstrap_index + 11] == (
        str(tmp_path),
        str(runtime.root),
        str(runtime.site_packages),
        runtime.site_packages_ref,
        runtime.interpreter_ref,
        str(tooling_site),
        "5" * 64,
        command[bootstrap_index + 8],
        identity.source_commit,
        identity.wheel_sha256,
    )
    assert command[command.index("-n") + 2 : command.index("-n") + 5] == (
        "-o",
        "pythonpath=",
        "--import-mode=importlib",
    )
    assert witness.artifact_origin_sha256 == hashlib.sha256(origin_raw[0]).hexdigest()
    assert witness.interpreter_ref == runtime.interpreter_ref
    assert witness.site_packages_ref == runtime.site_packages_ref
    assert witness.subprocess_policy == evidence._SUBPROCESS_POLICY  # noqa: SLF001
    assert witness.tooling_modules_sha256 == "4" * 64
    assert witness.tooling_policy == evidence._TEST_TOOLING_POLICY  # noqa: SLF001
    assert len(witness.tooling_snapshot_sha256 or "") == 64


@pytest.mark.parametrize(
    "parameter_id",
    (
        "",
        "../../private",
        "private/path",
        "secret token",
        "тело",
        "x" * 129,
        "safe][forged",
    ),
)
def test_test_ref_parameter_ids_are_bounded_privacy_safe_ascii(
    monkeypatch: pytest.MonkeyPatch,
    parameter_id: str,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_exact_git_blob",
        lambda *_args: b"def test_probe():\n    pass\n",
    )
    ref = f"tests/test_probe.py::test_probe[{parameter_id}]"
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^test_ref_invalid$"):
        evidence._test_source(ROOT, "a" * 40, ref)  # noqa: SLF001

    assert (
        evidence._test_source(  # noqa: SLF001
            ROOT,
            "a" * 40,
            "tests/test_probe.py::test_probe[safe-id:1.0]",
        )
        == b"def test_probe():\n    pass\n"
    )


@pytest.mark.parametrize(
    "path",
    (
        "tests/test_../private.py",
        "tests/test_probe token.py",
        "tests/тест_probe.py",
        "tests/test_probe.py/extra.py",
    ),
)
def test_test_ref_paths_are_bounded_repository_relative_ascii(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_exact_git_blob",
        lambda *_args: b"def test_probe():\n    pass\n",
    )
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^test_ref_invalid$"):
        evidence._test_source(ROOT, "a" * 40, f"{path}::test_probe")  # noqa: SLF001


@pytest.mark.parametrize("result", ("VERIFIED", "FAILED"))
def test_bundle_derives_canonical_body_free_manifest_and_paths(result: str) -> None:
    identity = _identity()
    journey_id = "conversation_recall"
    raw = _receipt(identity, journey_id, result=result)

    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        raw,
        identity=identity,
        journey_id=journey_id,
        evidence_class=CLEAN_CLASS,
    )
    release_binding = hashlib.sha256(
        json.dumps(
            identity.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    prefix = f"{journey_id}--clean_artifact--{result.lower()}--{release_binding}.json"
    assert bundle.receipt_ref == f"evidence/golden_journeys/receipts/{prefix}"
    assert bundle.manifest_ref == f"evidence/golden_journeys/manifests/{prefix}"
    assert bundle.receipt_sha256 == hashlib.sha256(raw).hexdigest()
    assert bundle.manifest_sha256 == hashlib.sha256(bundle.manifest).hexdigest()
    manifest = json.loads(bundle.manifest)
    receipt = json.loads(raw)
    assert bundle.manifest == evidence.canonical_json_bytes(manifest)
    assert manifest == {
        "$schema": evidence.MANIFEST_SCHEMA,
        "evidence_class": CLEAN_CLASS,
        "journey_id": journey_id,
        "observation": {
            "artifact_ref": bundle.receipt_ref,
            "artifact_schema": evidence.CLEAN_ARTIFACT_RECEIPT_SCHEMA,
            "artifact_sha256": bundle.receipt_sha256,
            "check_ids": receipt["check_ids"],
            "environment": "clean_artifact",
            "observed_at_utc": "2026-08-30T08:00:00Z",
        },
        "release": identity.payload(),
        "result": result,
    }
    serialized = bundle.receipt + bundle.manifest
    for private in (b"raw message", b"prompt", b"/home/", b"telegram:test:5001"):
        assert private not in serialized


def test_bundle_projection_rejects_non_timestamp_body_content() -> None:
    identity = _identity()
    payload = json.loads(_receipt(identity, "conversation_recall"))
    payload["observed_at_utc"] = "/home/private/raw-message"

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_receipt_invalid$"):
        evidence._bundle_from_receipt(  # noqa: SLF001
            evidence.canonical_json_bytes(payload),
            identity=identity,
            journey_id="conversation_recall",
            evidence_class=CLEAN_CLASS,
        )


def test_public_bundle_apis_accept_no_caller_claim_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = {
        "result",
        "outcome",
        "outcomes",
        "observed_at_utc",
        "check_ids",
        "environment",
        "receipt_ref",
        "manifest_ref",
        "artifact_sha256",
        "interpreter",
        "interpreter_ref",
        "tooling_site",
        "tooling_modules_sha256",
        "tooling_snapshot_sha256",
    }
    for function in (evidence.produce_evidence_bundle, evidence.manifest_from_receipt):
        assert forbidden.isdisjoint(inspect.signature(function).parameters)

    identity = _identity()
    raw = _receipt(identity, "conversation_recall")
    validations: list[bytes] = []

    def validate(candidate: bytes, **_kwargs: object) -> dict[str, object]:
        validations.append(candidate)
        return json.loads(candidate)

    monkeypatch.setattr(evidence, "validate_receipt", validate)
    bundle = evidence.manifest_from_receipt(
        raw,
        expected_release=identity,
        expected_journey_id="conversation_recall",
        expected_evidence_class=CLEAN_CLASS,
        repo_root=ROOT,
    )
    assert validations == [raw]
    assert bundle.result == "VERIFIED"


def test_bundle_publish_is_create_only_and_rolls_back_only_its_first_file(tmp_path: Path) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    complete = tmp_path / "complete"
    complete.mkdir(mode=0o700)

    published = evidence.write_evidence_bundle_exclusive(complete, bundle)

    receipt_path = complete / bundle.receipt_ref
    manifest_path = complete / bundle.manifest_ref
    assert receipt_path.read_bytes() == bundle.receipt
    assert manifest_path.read_bytes() == bundle.manifest
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert published["receipt_sha256"] == bundle.receipt_sha256
    assert published["manifest_sha256"] == bundle.manifest_sha256
    assert evidence.write_evidence_bundle_exclusive(complete, bundle) == published
    assert receipt_path.read_bytes() == bundle.receipt
    assert manifest_path.read_bytes() == bundle.manifest

    linked_recovery = tmp_path / "linked-recovery"
    linked_receipt = _private_bundle_path(linked_recovery, bundle.receipt_ref)
    interrupted_staging = linked_receipt.parent / f".{linked_receipt.name}.interrupted.tmp"
    interrupted_staging.write_bytes(bundle.receipt)
    interrupted_staging.chmod(0o600)
    os.link(interrupted_staging, linked_receipt)
    assert linked_receipt.stat().st_nlink == 2
    assert evidence.write_evidence_bundle_exclusive(linked_recovery, bundle) == published
    assert not interrupted_staging.exists()
    assert linked_receipt.stat().st_nlink == 1
    assert (linked_recovery / bundle.manifest_ref).read_bytes() == bundle.manifest

    unsafe_orphan = tmp_path / "unsafe-orphan"
    unsafe_receipt = _private_bundle_path(unsafe_orphan, bundle.receipt_ref)
    unsafe_receipt.write_bytes(bundle.receipt)
    unsafe_receipt.chmod(0o644)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(unsafe_orphan, bundle)
    assert unsafe_receipt.read_bytes() == bundle.receipt
    assert not (unsafe_orphan / bundle.manifest_ref).exists()

    collision = tmp_path / "collision"
    collision_manifest = _private_bundle_path(collision, bundle.manifest_ref)
    collision_manifest.write_bytes(b"pre-existing manifest")
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(collision, bundle)
    assert not (collision / bundle.receipt_ref).exists()
    assert collision_manifest.read_bytes() == b"pre-existing manifest"


def test_bundle_retry_reuses_first_timestamp_after_receipt_only_crash(tmp_path: Path) -> None:
    identity = _identity()
    first = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "output"
    receipt_path = _private_bundle_path(output, first.receipt_ref)
    receipt_path.write_bytes(first.receipt)
    receipt_path.chmod(0o600)

    restarted_payload = json.loads(first.receipt)
    restarted_payload["observed_at_utc"] = "2026-08-30T08:00:01Z"
    restarted = evidence._bundle_from_receipt(  # noqa: SLF001
        evidence.canonical_json_bytes(restarted_payload),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    assert restarted.receipt_ref == first.receipt_ref
    assert restarted.manifest_ref == first.manifest_ref
    assert restarted.receipt != first.receipt
    assert restarted.manifest != first.manifest

    published = evidence.write_evidence_bundle_exclusive(output, restarted)

    assert published == {
        "manifest_ref": first.manifest_ref,
        "manifest_sha256": first.manifest_sha256,
        "receipt_ref": first.receipt_ref,
        "receipt_sha256": first.receipt_sha256,
        "result": first.result,
    }
    assert receipt_path.read_bytes() == first.receipt
    assert (output / first.manifest_ref).read_bytes() == first.manifest


def test_bundle_retry_fsyncs_adopted_receipt_before_directory_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "adopted-receipt"
    receipt = _private_bundle_path(output, bundle.receipt_ref)
    receipt.write_bytes(bundle.receipt)
    receipt.chmod(0o600)
    manifest = output / bundle.manifest_ref
    events: list[tuple[str, Path]] = []
    real_fsync = evidence.os.fsync
    real_link = evidence.os.link

    def record_fsync(descriptor: int) -> None:
        kind = "file_fsync" if stat.S_ISREG(os.fstat(descriptor).st_mode) else "directory_fsync"
        events.append((kind, Path(os.readlink(f"/proc/self/fd/{descriptor}"))))
        real_fsync(descriptor)

    def record_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        target_path = Path(os.readlink(f"/proc/self/fd/{dst_dir_fd}")) / target
        if target_path == manifest:
            events.append(("manifest_link", target_path))
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(evidence.os, "fsync", record_fsync)
    monkeypatch.setattr(evidence.os, "link", record_link)

    evidence.write_evidence_bundle_exclusive(output, bundle)

    leaf_fsync = events.index(("file_fsync", receipt))
    manifest_link = events.index(("manifest_link", manifest))
    assert any(
        event == ("directory_fsync", receipt.parent)
        for event in events[leaf_fsync + 1 : manifest_link]
    )
    assert leaf_fsync < manifest_link


def test_adopted_leaf_keeps_one_byte_validated_descriptor_through_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "single-descriptor"
    receipt = _private_bundle_path(output, bundle.receipt_ref)
    receipt.write_bytes(bundle.receipt)
    receipt.chmod(0o600)
    parent_descriptor = os.open(
        receipt.parent,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    real_open = evidence.os.open
    final_name_opens = 0
    mutation = b"x" * len(bundle.receipt)

    def rewrite_on_second_final_name_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal final_name_opens
        if path == receipt.name and dir_fd == parent_descriptor:
            final_name_opens += 1
            if final_name_opens == 2:
                writer = real_open(
                    path,
                    os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC,
                    dir_fd=dir_fd,
                )
                try:
                    assert os.write(writer, mutation) == len(mutation)
                finally:
                    os.close(writer)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence.os, "open", rewrite_on_second_final_name_open)

    try:
        digest = evidence._require_exact_existing_bundle_file_at(  # noqa: SLF001
            parent_descriptor,
            receipt.name,
            bundle.receipt,
        )
    finally:
        os.close(parent_descriptor)

    assert final_name_opens == 1
    assert digest == bundle.receipt_sha256
    assert receipt.read_bytes() == bundle.receipt


def test_adopted_linked_leaf_rejects_name_swap_during_alias_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "alias-swap"
    receipt = _private_bundle_path(output, bundle.receipt_ref)
    alias = receipt.parent / f".{receipt.name}.interrupted.tmp"
    alias.write_bytes(bundle.receipt)
    alias.chmod(0o600)
    os.link(alias, receipt)
    replacement = b"x" * len(bundle.receipt)
    real_unlink = evidence.os.unlink
    swapped = False

    def swap_final_name_after_alias_unlink(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal swapped
        real_unlink(name, dir_fd=dir_fd)
        if name == alias.name and dir_fd is not None:
            swapped = True
            real_unlink(receipt.name, dir_fd=dir_fd)
            writer = os.open(
                receipt.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
            try:
                assert os.write(writer, replacement) == len(replacement)
            finally:
                os.close(writer)

    monkeypatch.setattr(evidence.os, "unlink", swap_final_name_after_alias_unlink)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert swapped
    assert receipt.read_bytes() == replacement
    assert not (output / bundle.manifest_ref).exists()


def test_bundle_retry_rejects_exact_manifest_without_receipt(tmp_path: Path) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "manifest-only"
    manifest = _private_bundle_path(output, bundle.manifest_ref)
    manifest.write_bytes(bundle.manifest)
    manifest.chmod(0o600)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert manifest.read_bytes() == bundle.manifest
    assert not (output / bundle.receipt_ref).exists()


def test_bundle_retry_rejects_non_timestamp_drift(tmp_path: Path) -> None:
    identity = _identity()
    first = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "output"
    receipt_path = _private_bundle_path(output, first.receipt_ref)
    receipt_path.write_bytes(first.receipt)
    receipt_path.chmod(0o600)
    adversarial_alias = receipt_path.parent / f".{receipt_path.name}.adversarial.tmp"
    os.link(receipt_path, adversarial_alias)
    assert receipt_path.stat().st_nlink == 2
    drifted_payload = json.loads(first.receipt)
    drifted_payload["observed_at_utc"] = "2026-08-30T08:00:01Z"
    drifted_payload["execution"]["outcome_projection_sha256"] = "0" * 64
    drifted = evidence._bundle_from_receipt(  # noqa: SLF001
        evidence.canonical_json_bytes(drifted_payload),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, drifted)

    assert receipt_path.read_bytes() == first.receipt
    assert adversarial_alias.read_bytes() == first.receipt
    assert receipt_path.stat().st_nlink == 2
    assert not (output / first.manifest_ref).exists()


def test_bundle_final_names_appear_only_after_complete_staging_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    real_link = evidence.os.link
    linked: list[Path] = []

    def atomic_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        source_path = Path(os.readlink(f"/proc/self/fd/{src_dir_fd}")) / source
        target_path = Path(os.readlink(f"/proc/self/fd/{dst_dir_fd}")) / target
        expected = bundle.receipt if "receipts" in target_path.parts else bundle.manifest
        assert source_path.name.startswith(f".{target_path.name}.")
        assert source_path.name.endswith(".tmp")
        assert source_path.read_bytes() == expected
        assert not target_path.exists()
        linked.append(target_path)
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(evidence.os, "link", atomic_link)

    evidence.write_evidence_bundle_exclusive(output, bundle)

    assert linked == [output / bundle.receipt_ref, output / bundle.manifest_ref]


def test_bundle_empty_root_durably_orders_ancestors_receipt_then_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "ordered"
    output.mkdir(mode=0o700)
    events: list[tuple[str, Path]] = []
    real_fsync = evidence.os.fsync
    real_link = evidence.os.link

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append(("fsync", Path(os.readlink(f"/proc/self/fd/{descriptor}"))))
        real_fsync(descriptor)

    def record_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        target_path = Path(os.readlink(f"/proc/self/fd/{dst_dir_fd}")) / target
        events.append(("link", target_path))
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(evidence.os, "fsync", record_fsync)
    monkeypatch.setattr(evidence.os, "link", record_link)

    evidence.write_evidence_bundle_exclusive(output, bundle)

    receipt = output / bundle.receipt_ref
    manifest = output / bundle.manifest_ref
    receipt_link = events.index(("link", receipt))
    manifest_link = events.index(("link", manifest))
    for directory in (
        output.parent,
        output,
        output / "evidence",
        output / "evidence/golden_journeys",
        receipt.parent,
        manifest.parent,
    ):
        assert events.index(("fsync", directory)) < receipt_link
    assert any(event == ("fsync", receipt.parent) for event in events[receipt_link + 1 : manifest_link])


@pytest.mark.parametrize("failed_directory", ["root", "parent"])
def test_bundle_root_and_parent_fsync_failure_precedes_namespace_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_directory: str,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / f"fsync-{failed_directory}"
    output.mkdir(mode=0o700)
    target = output if failed_directory == "root" else output.parent
    real_fsync = evidence.os.fsync
    failed = False

    def fail_selected_directory(descriptor: int) -> None:
        nonlocal failed
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not failed and path == target:
            failed = True
            raise OSError("injected root authority fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence.os, "fsync", fail_selected_directory)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert failed
    assert list(output.iterdir()) == []


def test_bundle_ancestor_fsync_failure_publishes_no_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "fsync-failure"
    output.mkdir(mode=0o700)
    real_fsync = evidence.os.fsync
    failed = False

    def fail_ancestor(descriptor: int) -> None:
        nonlocal failed
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not failed and path.name == "golden_journeys":
            failed = True
            raise OSError("injected ancestor fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence.os, "fsync", fail_ancestor)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert failed
    assert not (output / bundle.receipt_ref).exists()
    assert not (output / bundle.manifest_ref).exists()


def test_bundle_publication_stays_on_pinned_root_and_rejects_lexical_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "pinned"
    displaced = tmp_path / "displaced"
    output.mkdir(mode=0o700)
    real_link = evidence.os.link
    swapped = False

    def swap_after_first_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal swapped
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not swapped:
            swapped = True
            output.rename(displaced)
            output.mkdir(mode=0o700)

    monkeypatch.setattr(evidence.os, "link", swap_after_first_link)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert swapped
    assert list(output.iterdir()) == []
    assert not (displaced / bundle.receipt_ref).exists()
    assert not (displaced / bundle.manifest_ref).exists()


def test_bundle_publication_rejects_and_cleans_up_rebound_child_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    output = tmp_path / "pinned"
    output.mkdir(mode=0o700)
    receipt_parent = output / Path(bundle.receipt_ref).parent
    displaced_parent = receipt_parent.with_name("receipts-displaced")
    real_link = evidence.os.link
    swapped = False

    def swap_child_after_first_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal swapped
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not swapped:
            swapped = True
            receipt_parent.rename(displaced_parent)
            receipt_parent.mkdir(mode=0o700)

    monkeypatch.setattr(evidence.os, "link", swap_child_after_first_link)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    assert swapped
    assert list(receipt_parent.iterdir()) == []
    assert not (displaced_parent / Path(bundle.receipt_ref).name).exists()
    assert not (output / bundle.manifest_ref).exists()


def test_bundle_publication_rejects_absolute_ancestor_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    anchor = tmp_path / "anchor"
    private_parent = anchor / "private"
    output = private_parent / "output"
    output.mkdir(mode=0o700, parents=True)
    anchor.chmod(0o700)
    private_parent.chmod(0o700)
    displaced_anchor = tmp_path / "displaced-anchor"
    real_link = evidence.os.link
    swapped = False

    def swap_absolute_ancestor_after_first_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal swapped
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not swapped:
            swapped = True
            anchor.rename(displaced_anchor)
            output.mkdir(mode=0o700, parents=True)
            anchor.chmod(0o700)
            private_parent.chmod(0o700)

    monkeypatch.setattr(evidence.os, "link", swap_absolute_ancestor_after_first_link)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(output, bundle)

    displaced_output = displaced_anchor / "private/output"
    assert swapped
    assert list(output.iterdir()) == []
    assert not (displaced_output / bundle.receipt_ref).exists()
    assert not (displaced_output / bundle.manifest_ref).exists()


def test_bundle_publication_rechecks_private_parent_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    parent = tmp_path / "private-parent"
    output = parent / "output"
    parent.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    real_link = evidence.os.link
    changed = False

    def chmod_parent_after_first_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal changed
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not changed:
            changed = True
            parent.chmod(0o755)

    monkeypatch.setattr(evidence.os, "link", chmod_parent_after_first_link)

    try:
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
            evidence.write_evidence_bundle_exclusive(output, bundle)
    finally:
        parent.chmod(0o700)

    assert changed
    assert not (output / bundle.receipt_ref).exists()
    assert not (output / bundle.manifest_ref).exists()


@pytest.mark.parametrize("cleanup_failure", ["unlink", "fsync"])
def test_bundle_rollback_preserves_receipt_when_manifest_cleanup_is_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    parent = tmp_path / "private-parent"
    output = parent / "output"
    parent.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    receipt = output / bundle.receipt_ref
    manifest = output / bundle.manifest_ref
    real_link = evidence.os.link
    real_unlink = evidence.os.unlink
    real_fsync = evidence.os.fsync
    manifest_linked = False

    def invalidate_parent_after_manifest_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal manifest_linked
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        target_path = Path(os.readlink(f"/proc/self/fd/{dst_dir_fd}")) / target
        if target_path == manifest:
            manifest_linked = True
            parent.chmod(0o755)

    def fail_manifest_unlink(name: str, *, dir_fd: int | None = None) -> None:
        if (
            cleanup_failure == "unlink"
            and manifest_linked
            and name == manifest.name
            and dir_fd is not None
            and Path(os.readlink(f"/proc/self/fd/{dir_fd}")) == manifest.parent
        ):
            raise OSError("injected manifest rollback unlink failure")
        real_unlink(name, dir_fd=dir_fd)

    def fail_manifest_absence_fsync(descriptor: int) -> None:
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if (
            cleanup_failure == "fsync"
            and manifest_linked
            and descriptor_path == manifest.parent
            and not manifest.exists()
        ):
            raise OSError("injected manifest rollback fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(evidence.os, "link", invalidate_parent_after_manifest_link)
    monkeypatch.setattr(evidence.os, "unlink", fail_manifest_unlink)
    monkeypatch.setattr(evidence.os, "fsync", fail_manifest_absence_fsync)

    try:
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
            evidence.write_evidence_bundle_exclusive(output, bundle)
    finally:
        parent.chmod(0o700)

    assert manifest_linked
    assert receipt.read_bytes() == bundle.receipt
    if cleanup_failure == "unlink":
        assert manifest.read_bytes() == bundle.manifest
    else:
        assert not manifest.exists()


def test_bundle_rollback_preserves_receipt_after_unknown_manifest_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    parent = tmp_path / "private-parent"
    output = parent / "output"
    parent.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    receipt = output / bundle.receipt_ref
    manifest = output / bundle.manifest_ref
    real_link = evidence.os.link
    real_fsync = evidence.os.fsync
    real_cleanup = evidence._cleanup_owned_target_at  # noqa: SLF001
    manifest_linked = False
    injected_write_failure = False

    def invalidate_parent_after_manifest_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal manifest_linked
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        target_path = Path(os.readlink(f"/proc/self/fd/{dst_dir_fd}")) / target
        if target_path == manifest:
            manifest_linked = True
            parent.chmod(0o755)

    def fail_first_manifest_directory_fsync(descriptor: int) -> None:
        nonlocal injected_write_failure
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if (
            manifest_linked
            and not injected_write_failure
            and descriptor_path == manifest.parent
            and manifest.exists()
        ):
            injected_write_failure = True
            raise OSError("injected manifest write fsync failure")
        real_fsync(descriptor)

    def lose_internal_manifest_cleanup(
        parent_descriptor: int,
        name: str,
        owned_identity: tuple[int, int] | None,
    ) -> None:
        if manifest_linked and name == manifest.name:
            return
        real_cleanup(parent_descriptor, name, owned_identity)

    monkeypatch.setattr(evidence.os, "link", invalidate_parent_after_manifest_link)
    monkeypatch.setattr(evidence.os, "fsync", fail_first_manifest_directory_fsync)
    monkeypatch.setattr(evidence, "_cleanup_owned_target_at", lose_internal_manifest_cleanup)

    try:
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
            evidence.write_evidence_bundle_exclusive(output, bundle)
    finally:
        parent.chmod(0o700)

    assert manifest_linked
    assert injected_write_failure
    assert receipt.read_bytes() == bundle.receipt
    assert manifest.read_bytes() == bundle.manifest


def test_bundle_root_and_parent_must_be_private_physical_directories(tmp_path: Path) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    public_root = tmp_path / "public-root"
    public_root.mkdir(mode=0o755)
    public_root.chmod(0o755)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(public_root, bundle)

    public_parent = tmp_path / "public-parent"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)
    private_child = public_parent / "private-child"
    private_child.mkdir(mode=0o700)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(private_child, bundle)

    physical = tmp_path / "physical"
    physical.mkdir(mode=0o700)
    linked = tmp_path / "linked-root"
    linked.symlink_to(physical, target_is_directory=True)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(linked, bundle)


def test_bundle_authority_and_canonical_json_fail_closed(tmp_path: Path) -> None:
    identity = _identity()
    bundle = evidence._bundle_from_receipt(  # noqa: SLF001
        _receipt(identity, "conversation_recall"),
        identity=identity,
        journey_id="conversation_recall",
        evidence_class=CLEAN_CLASS,
    )
    forged = replace(bundle, receipt_sha256="0" * 64)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^evidence_bundle_invalid$"):
        evidence.write_evidence_bundle_exclusive(tmp_path, forged)

    duplicate = b'{"$schema":"one","$schema":"two"}'
    non_finite = b'{"value":NaN}'
    for raw in (duplicate, non_finite, b'{"value":1}\n'):
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^manifest_json_invalid$"):
            evidence._load_canonical_manifest(raw)  # noqa: SLF001


def test_clean_artifact_requires_deterministic_bundle_cli() -> None:
    bundle_args = [
        "bundle",
        "--release-root",
        "/release",
        "--repo-root",
        "/repo",
        "--journey-id",
        "conversation_recall",
        "--output-root",
        "/repo",
    ]
    parsed = evidence.build_parser().parse_args(bundle_args)
    assert parsed.command == "bundle"
    with pytest.raises(SystemExit):
        evidence.build_parser().parse_args(
            [
                "run",
                "--release-root",
                "/release",
                "--repo-root",
                "/repo",
                "--journey-id",
                "conversation_recall",
                "--evidence-class",
                CLEAN_CLASS,
                "--output",
                "/arbitrary.json",
            ]
        )


def test_bundle_output_root_must_stay_outside_the_exact_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    nested = repository / "output"
    external = tmp_path / "external"
    nested.mkdir(parents=True)
    external.mkdir()

    assert evidence._external_bundle_output_root(repository, external) == external  # noqa: SLF001
    for invalid in (repository, nested):
        with pytest.raises(
            evidence.ExactReleaseEvidenceError,
            match="^bundle_output_must_be_external$",
        ):
            evidence._external_bundle_output_root(repository, invalid)  # noqa: SLF001


def test_clean_artifact_cannot_escape_through_receipt_only_publication(
    tmp_path: Path,
) -> None:
    raw = _receipt(_identity(), "conversation_recall")
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="^clean_artifact_bundle_required$",
    ):
        evidence.write_receipt_exclusive(tmp_path / "receipt.json", raw)
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="^clean_artifact_bundle_required$",
    ):
        evidence.produce_receipt(
            repo_root=tmp_path,
            release_root=tmp_path,
            journey_id="conversation_recall",
            evidence_class=CLEAN_CLASS,
        )
