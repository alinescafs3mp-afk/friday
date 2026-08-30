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
from dataclasses import replace
from pathlib import Path

import pytest

from tools import exact_release_evidence as evidence

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
                    "origin_report_sha256": "1" * 64,
                    "site_packages_ref": "venv/lib/python3.14/site-packages",
                    "subprocess_policy": evidence._SUBPROCESS_POLICY,  # noqa: SLF001
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
        site_packages_ref=runtime.site_packages_ref,
        subprocess_policy=evidence._SUBPROCESS_POLICY,  # noqa: SLF001
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


def _write_release_runtime_shape(root: Path) -> Path:
    site = root / "venv/lib/python3.14/site-packages"
    (site / "friday").mkdir(parents=True)
    (site / "friday/__init__.py").write_text("ORIGIN = 'sealed'\n", encoding="ascii")
    (site / "friday-1.0.dist-info").mkdir()
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
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(linked)  # noqa: SLF001

    mismatched_python = tmp_path / "mismatched-python"
    mismatched_site = mismatched_python / "venv/lib/python3.13/site-packages"
    (mismatched_site / "friday").mkdir(parents=True)
    (mismatched_site / "friday/__init__.py").write_text("", encoding="ascii")
    (mismatched_site / "friday-1.0.dist-info").mkdir()
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_runtime_invalid$"):
        evidence._authenticate_release_runtime(mismatched_python)  # noqa: SLF001


def _write_bootstrap_fixture(tmp_path: Path, test_body: str) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    release = tmp_path / "release"
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
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            evidence._INSTALLED_PYTEST_BOOTSTRAP,  # noqa: SLF001
            str(source),
            str(release),
            str(site),
            site.relative_to(release).as_posix(),
            str(report),
            "a" * 40,
            "c" * 64,
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            f"--basetemp={source / 'pytest-tmp'}",
            str(probe),
        ),
        cwd=source,
        check=False,
        capture_output=True,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        timeout=60,
    )


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
    assert payload["site_packages_ref"] == "venv/lib/python3.14/site-packages"
    assert payload["source_commit"] == "a" * 40
    assert payload["wheel_sha256"] == "c" * 64
    assert payload["subprocess_policy"] == evidence._SUBPROCESS_POLICY  # noqa: SLF001
    assert payload["module_count"] >= 2


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


def test_closed_clean_runner_binds_the_installed_bootstrap_and_origin_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    runtime = _release_runtime(tmp_path / "release", identity)
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

    def run_pytest(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        report = Path(next(item[11:] for item in command if item.startswith("--junitxml=")))
        collection = Path(
            next(item[29:] for item in command if item.startswith("--friday-collection-manifest="))
        )
        origin = Path(command[10])
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
                "module_count": 2,
                "module_origins_sha256": "3" * 64,
                "schema": "friday.clean-artifact-import-origin.v1",
                "site_packages_ref": runtime.site_packages_ref,
                "source_commit": identity.source_commit,
                "subprocess_policy": evidence._SUBPROCESS_POLICY,  # noqa: SLF001
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
    assert command[:6] == (
        sys.executable,
        "-I",
        "-X",
        command[3],
        "-c",
        evidence._INSTALLED_PYTEST_BOOTSTRAP,  # noqa: SLF001
    )
    assert command[6:13] == (
        str(tmp_path),
        str(runtime.root),
        str(runtime.site_packages),
        runtime.site_packages_ref,
        command[10],
        identity.source_commit,
        identity.wheel_sha256,
    )
    assert command[command.index("-n") + 2 : command.index("-n") + 5] == (
        "-o",
        "pythonpath=",
        "--import-mode=importlib",
    )
    assert witness.artifact_origin_sha256 == hashlib.sha256(origin_raw[0]).hexdigest()
    assert witness.site_packages_ref == runtime.site_packages_ref
    assert witness.subprocess_policy == evidence._SUBPROCESS_POLICY  # noqa: SLF001


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
    complete.mkdir()

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

    recovered = tmp_path / "recovered"
    recovered_receipt = recovered / bundle.receipt_ref
    recovered_receipt.parent.mkdir(parents=True)
    recovered_receipt.write_bytes(bundle.receipt)
    recovered_receipt.chmod(0o600)
    recovered_publish = evidence.write_evidence_bundle_exclusive(recovered, bundle)
    assert recovered_publish == published
    assert (recovered / bundle.manifest_ref).read_bytes() == bundle.manifest

    linked_recovery = tmp_path / "linked-recovery"
    linked_receipt = linked_recovery / bundle.receipt_ref
    linked_receipt.parent.mkdir(parents=True)
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
    unsafe_receipt = unsafe_orphan / bundle.receipt_ref
    unsafe_receipt.parent.mkdir(parents=True)
    unsafe_receipt.write_bytes(bundle.receipt)
    unsafe_receipt.chmod(0o644)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(unsafe_orphan, bundle)
    assert unsafe_receipt.read_bytes() == bundle.receipt
    assert not (unsafe_orphan / bundle.manifest_ref).exists()

    collision = tmp_path / "collision"
    collision.mkdir()
    collision_manifest = collision / bundle.manifest_ref
    collision_manifest.parent.mkdir(parents=True)
    collision_manifest.write_bytes(b"pre-existing manifest")
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^bundle_output_invalid$"):
        evidence.write_evidence_bundle_exclusive(collision, bundle)
    assert not (collision / bundle.receipt_ref).exists()
    assert collision_manifest.read_bytes() == b"pre-existing manifest"


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
    output.mkdir()
    real_link = evidence.os.link
    linked: list[Path] = []

    def atomic_link(source: Path, target: Path, *, follow_symlinks: bool) -> None:
        source_path = Path(source)
        target_path = Path(target)
        expected = bundle.receipt if "receipts" in target_path.parts else bundle.manifest
        assert source_path.name.startswith(f".{target_path.name}.")
        assert source_path.name.endswith(".tmp")
        assert source_path.read_bytes() == expected
        assert not target_path.exists()
        linked.append(target_path)
        real_link(source_path, target_path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(evidence.os, "link", atomic_link)

    evidence.write_evidence_bundle_exclusive(output, bundle)

    assert linked == [output / bundle.receipt_ref, output / bundle.manifest_ref]


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
