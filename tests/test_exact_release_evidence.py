from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path

import pytest

from tools import exact_release_evidence as evidence

JOURNEY_ID = "conversation_recall"
EVIDENCE_CLASS = "deterministic contract"
TEST_SOURCE_PATH = "tests/test_message_window_runtime_integration.py"
TEST_FUNCTION = "test_promoted_exact_window_is_deterministic_scoped_and_receipted"
TEST_REF_BASE = f"{TEST_SOURCE_PATH}::{TEST_FUNCTION}"


@dataclass(frozen=True, slots=True)
class ExactRepository:
    root: Path
    identity: evidence.ReleaseIdentity
    refs: tuple[str, ...]


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def exact_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExactRepository:
    root = tmp_path / "repository"
    producer = root / evidence.PRODUCER_PATH
    test_source = root / TEST_SOURCE_PATH
    producer.parent.mkdir(parents=True)
    test_source.parent.mkdir(parents=True)

    current_producer = Path(evidence.__file__).read_bytes()
    producer.write_bytes(current_producer)
    test_source.write_text(
        f"def {TEST_FUNCTION}():\n    pass\n",
        encoding="utf-8",
    )

    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Exact Evidence Test")
    _git(root, "config", "user.email", "exact-evidence@example.invalid")
    _git(root, "add", evidence.PRODUCER_PATH, test_source.relative_to(root).as_posix())
    _git(root, "commit", "-q", "-m", "exact evidence fixture")
    commit = _git(root, "rev-parse", "HEAD")

    monkeypatch.setattr(evidence, "__file__", str(producer))
    return ExactRepository(
        root=root,
        identity=evidence.ReleaseIdentity(
            source_commit=commit,
            tree_sha256="a" * 64,
            wheel_sha256="b" * 64,
            database_schema=46,
        ),
        refs=evidence.proof_refs(JOURNEY_ID, EVIDENCE_CLASS),
    )


def _collection_sha256(refs: tuple[str, ...]) -> str:
    payload = evidence.canonical_json_bytes({"nodeids": list(refs), "version": 1})
    return hashlib.sha256(payload).hexdigest()


def _produce(
    repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: tuple[str, ...] | None = None,
    owner_smoke: evidence.AuthenticatedOwnerSmokeBinding | None = None,
) -> bytes:
    if outcomes is None:
        outcomes = ("PASSED",) * len(repository.refs)
    assert len(outcomes) == len(repository.refs)
    exit_code = 0 if all(outcome == "PASSED" for outcome in outcomes) else 1

    def execute_closed_inventory(
        repo_root: Path,
        identity: evidence.ReleaseIdentity,
        journey_id: str,
        evidence_class: str,
        *,
        require_running_producer: bool = True,
    ) -> evidence._ExecutionWitness:  # noqa: SLF001 - exact internal producer witness
        assert repo_root == repository.root
        assert identity == repository.identity
        assert (journey_id, evidence_class) == (JOURNEY_ID, EVIDENCE_CLASS)
        assert require_running_producer is True
        return evidence._execution_witness(  # noqa: SLF001 - code-owned test boundary
            outcomes,
            exit_code,
            _collection_sha256(repository.refs),
            evidence._outcome_projection_sha256(repository.refs, outcomes),  # noqa: SLF001
        )

    monkeypatch.setattr(evidence, "_run_closed_pytest", execute_closed_inventory)
    return evidence._produce_for_identity(
        repo_root=repository.root,
        identity=repository.identity,
        journey_id=JOURNEY_ID,
        evidence_class=EVIDENCE_CLASS,
        owner_smoke=owner_smoke,
    )


def _payload(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _canonical(value: object) -> bytes:
    return evidence.canonical_json_bytes(value)


def _write_junit(path: Path, nodeids: tuple[str, ...], outcomes: tuple[str, ...]) -> None:
    assert len(nodeids) == len(outcomes)
    failures = outcomes.count("failure")
    errors = outcomes.count("error")
    skipped = outcomes.count("skipped")
    testcases = []
    for nodeid, outcome in zip(nodeids, outcomes, strict=True):
        terminal = "" if outcome == "passed" else f"<{outcome}/>"
        testcases.append(
            '<testcase name="synthetic"><properties>'
            f'<property name="friday_nodeid" value="{escape(nodeid, quote=True)}"/>'
            f"</properties>{terminal}</testcase>"
        )
    path.write_text(
        f'<testsuite tests="{len(nodeids)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}">{"".join(testcases)}</testsuite>',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("failed_index", "expected_result", "expected_exit"),
    [
        (None, "VERIFIED", 0),
        (1, "FAILED", 1),
    ],
)
def test_producer_derives_result_from_machine_outcomes(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
    failed_index: int | None,
    expected_result: str,
    expected_exit: int,
) -> None:
    outcomes = tuple(
        "FAILED" if index == failed_index else "PASSED" for index, _ref in enumerate(exact_repository.refs)
    )
    receipt = _payload(_produce(exact_repository, monkeypatch, outcomes=outcomes))
    source_sha256 = hashlib.sha256((exact_repository.root / TEST_SOURCE_PATH).read_bytes()).hexdigest()

    assert receipt["result"] == expected_result
    assert receipt["proofs"] == [
        {
            "outcome": outcome,
            "runner": "pytest",
            "test_ref": test_ref,
            "test_source_sha256": source_sha256,
        }
        for test_ref, outcome in zip(exact_repository.refs, outcomes, strict=True)
    ]
    assert isinstance(receipt["execution"], dict)
    assert receipt["execution"]["exit_code"] == expected_exit


def test_parameterized_test_refs_bind_one_committed_base_function(
    exact_repository: ExactRepository,
) -> None:
    source = (exact_repository.root / TEST_SOURCE_PATH).read_bytes()

    assert exact_repository.refs == evidence.proof_refs(JOURNEY_ID, EVIDENCE_CLASS)
    assert len(exact_repository.refs) > 1
    assert all(ref.startswith(f"{TEST_REF_BASE}[") and ref.endswith("]") for ref in exact_repository.refs)
    assert source.count(f"def {TEST_FUNCTION}(".encode()) == 1
    assert tuple(
        evidence._test_source(exact_repository.root, exact_repository.identity.source_commit, ref)
        for ref in exact_repository.refs
    ) == (source,) * len(exact_repository.refs)


def test_public_api_and_cli_have_no_caller_supplied_outcome() -> None:
    forbidden = {"result", "outcome", "outcomes", "exit_code"}
    for function in (evidence._produce_for_identity, evidence.produce_receipt):
        assert forbidden.isdisjoint(inspect.signature(function).parameters)
    assert "execution_witness" not in inspect.signature(evidence.validate_receipt).parameters

    arguments = [
        "run",
        "--release-root",
        "/release",
        "--repo-root",
        "/repository",
        "--journey-id",
        JOURNEY_ID,
        "--evidence-class",
        EVIDENCE_CLASS,
        "--output",
        "/receipt.json",
    ]
    evidence.build_parser().parse_args(arguments)
    for option in ("--result", "--outcome", "--exit-code"):
        with pytest.raises(SystemExit):
            evidence.build_parser().parse_args([*arguments, option, "PASSED"])


def test_release_payload_rejects_a_subclass_that_bypassed_post_init(
    exact_repository: ExactRepository,
) -> None:
    class ForgedReleaseIdentity(evidence.ReleaseIdentity):
        pass

    forged = object.__new__(ForgedReleaseIdentity)
    object.__setattr__(forged, "source_commit", exact_repository.identity.source_commit)
    object.__setattr__(forged, "tree_sha256", exact_repository.identity.tree_sha256)
    object.__setattr__(forged, "wheel_sha256", exact_repository.identity.wheel_sha256)
    object.__setattr__(forged, "database_schema", exact_repository.identity.database_schema)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_identity_invalid$"):
        evidence._release_payload(forged)


def test_owner_smoke_payload_rejects_a_subclass_that_bypassed_construction() -> None:
    class ForgedOwnerSmoke(evidence.AuthenticatedOwnerSmokeBinding):
        pass

    forged = object.__new__(ForgedOwnerSmoke)
    object.__setattr__(forged, "schema", "friday.owner-telegram-smoke.v1")
    object.__setattr__(forged, "authority", "owner.telegram")
    object.__setattr__(forged, "artifact_ref", "evidence/owner_smoke/receipt.json")
    object.__setattr__(forged, "artifact_sha256", "d" * 64)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^owner_smoke_not_authenticated$"):
        evidence._owner_smoke_payload(forged)


def test_public_producer_rejects_post_run_release_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = evidence.ReleaseIdentity(
        source_commit="a" * 40,
        tree_sha256="b" * 64,
        wheel_sha256="c" * 64,
        database_schema=46,
    )
    after = evidence.ReleaseIdentity(
        source_commit=before.source_commit,
        tree_sha256="d" * 64,
        wheel_sha256=before.wheel_sha256,
        database_schema=before.database_schema,
    )
    release_root = tmp_path / "release"
    repo_root = tmp_path / "repository"
    derived = iter((before, after))
    produced_identities: list[evidence.ReleaseIdentity] = []

    def derive_identity(candidate: Path) -> evidence.ReleaseIdentity:
        assert candidate == release_root
        return next(derived)

    def private_producer(
        *,
        repo_root: Path,
        identity: evidence.ReleaseIdentity,
        journey_id: str,
        evidence_class: str,
        owner_smoke: evidence.AuthenticatedOwnerSmokeBinding | None,
    ) -> bytes:
        assert repo_root == tmp_path / "repository"
        assert (journey_id, evidence_class) == (JOURNEY_ID, EVIDENCE_CLASS)
        assert owner_smoke is None
        produced_identities.append(identity)
        return b"private-producer-result"

    monkeypatch.setattr(evidence, "derive_release_identity", derive_identity)
    monkeypatch.setattr(evidence, "_produce_for_identity", private_producer)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^release_identity_changed$"):
        evidence.produce_receipt(
            repo_root=repo_root,
            release_root=release_root,
            journey_id=JOURNEY_ID,
            evidence_class=EVIDENCE_CLASS,
        )

    assert produced_identities == [before]


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (("passed",), ("PASSED",)),
        (("failure",), ("FAILED",)),
        (("passed", "failure"), ("PASSED", "FAILED")),
    ],
)
def test_strict_junit_derives_each_machine_outcome(
    tmp_path: Path,
    reported: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    nodeids = tuple(f"tests/test_machine.py::test_case_{index}" for index in range(len(reported)))
    report = tmp_path / "results.xml"
    _write_junit(report, nodeids, reported)

    assert evidence._pytest_outcomes(report, nodeids) == expected


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "skip", "error"])
def test_strict_junit_rejects_incomplete_or_non_test_failure_reports(
    tmp_path: Path,
    case: str,
) -> None:
    one = "tests/test_machine.py::test_one"
    two = "tests/test_machine.py::test_two"
    expected: tuple[str, ...] = (one, two)
    nodeids: tuple[str, ...] = expected
    outcomes: tuple[str, ...] = ("passed", "passed")
    if case == "missing":
        nodeids, outcomes = (one,), ("passed",)
    elif case == "extra":
        nodeids, outcomes = (
            (*expected, "tests/test_machine.py::test_three"),
            (
                "passed",
                "passed",
                "passed",
            ),
        )
    elif case == "duplicate":
        nodeids, outcomes = (one, one), ("passed", "passed")
        expected = (one,)
    elif case == "skip":
        outcomes = ("passed", "skipped")
    elif case == "error":
        outcomes = ("passed", "error")
    report = tmp_path / f"{case}.xml"
    _write_junit(report, nodeids, outcomes)

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^pytest_report_invalid$"):
        evidence._pytest_outcomes(report, expected)


def test_closed_runner_uses_hermetic_plugins_and_exact_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = evidence.proof_refs(JOURNEY_ID, EVIDENCE_CLASS)
    identity = evidence.ReleaseIdentity(
        source_commit="a" * 40,
        tree_sha256="b" * 64,
        wheel_sha256="c" * 64,
        database_schema=46,
    )
    manifest_refs = [refs]
    commands: list[tuple[str, ...]] = []
    environments: list[dict[str, str]] = []

    @contextmanager
    def isolated_environment():
        yield {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0",
            "PYTEST_PLUGINS": "ambient.untrusted_plugin",
            "PYTHONPYCACHEPREFIX": "/ambient/cache",
            "SENTINEL": "preserved",
        }

    def run_pytest(
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        stdout: int,
        stderr: int,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd == tmp_path
        assert check is False
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.DEVNULL
        assert timeout == evidence.PYTEST_TIMEOUT_SECONDS
        report = Path(
            next(item.removeprefix("--junitxml=") for item in command if item.startswith("--junitxml="))
        )
        collection = Path(
            next(
                item.removeprefix("--friday-collection-manifest=")
                for item in command
                if item.startswith("--friday-collection-manifest=")
            )
        )
        collection.write_bytes(
            evidence.canonical_json_bytes({"nodeids": list(manifest_refs[0]), "version": 1})
        )
        _write_junit(report, refs, ("passed",) * len(refs))
        commands.append(command)
        environments.append(dict(env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(evidence.quality_gate, "_isolated_test_environment", isolated_environment)
    monkeypatch.setattr(evidence.subprocess, "run", run_pytest)
    monkeypatch.setattr(evidence, "_require_exact_checkout", lambda *_arguments: None)
    monkeypatch.setattr(
        evidence,
        "_source_proofs",
        lambda *_arguments, **_keywords: ("0" * 64, []),
    )

    witness = evidence._run_closed_pytest(
        tmp_path,
        identity,
        JOURNEY_ID,
        EVIDENCE_CLASS,
    )

    command = commands[0]
    assert command[1:3] == ("-I", "-X")
    assert command[3].startswith("pycache_prefix=")
    python_cache = Path(command[3].removeprefix("pycache_prefix="))
    assert python_cache.name == "python-cache"
    expected_prefix = (
        sys.executable,
        "-I",
        "-X",
        f"pycache_prefix={python_cache}",
        "-c",
        evidence._PYTEST_BOOTSTRAP,  # noqa: SLF001 - exact code-first launcher
        str(tmp_path),
        "-q",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "anyio.pytest_plugin",
        "-p",
        "xdist.plugin",
        "-p",
        "tools.quality_gate",
        "-n",
        "0",
    )
    dynamic_options = command[len(expected_prefix) : -len(refs)]
    assert command[: len(expected_prefix)] == expected_prefix
    assert "sys.path.insert(0,root)" in evidence._PYTEST_BOOTSTRAP  # noqa: SLF001
    assert len(dynamic_options) == 3
    assert dynamic_options[0].startswith("--junitxml=")
    assert dynamic_options[1].startswith("--friday-collection-manifest=")
    assert dynamic_options[2].startswith("--basetemp=")
    assert command[-len(refs) :] == refs
    assert environments == [
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPYCACHEPREFIX": str(python_cache),
            "SENTINEL": "preserved",
        }
    ]
    assert witness.outcomes == ("PASSED",) * len(refs)
    assert witness.exit_code == 0
    expected_collection = evidence.canonical_json_bytes({"nodeids": list(refs), "version": 1})
    assert witness.collection_sha256 == hashlib.sha256(expected_collection).hexdigest()
    assert witness.outcome_projection_sha256 == evidence._outcome_projection_sha256(  # noqa: SLF001
        refs,
        witness.outcomes,
    )

    manifest_refs[0] = refs[:-1]
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^pytest_collection_invalid$"):
        evidence._run_closed_pytest(tmp_path, identity, JOURNEY_ID, EVIDENCE_CLASS)


def test_ignored_checkout_scan_allows_only_neutralized_regular_cache_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = (
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/CACHEDIR.TAG",
        ".mypy_cache/3.14/cache.db",
        "friday/__pycache__/module.cpython-314.pyc",
    )
    for path_text in allowed:
        path = tmp_path / path_text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cache")

    ignored = [b"\0".join(path.encode() for path in allowed) + b"\0"]
    monkeypatch.setattr(evidence, "_git", lambda *_arguments: ignored[0])
    evidence._require_neutralized_ignored_files(tmp_path)

    forbidden = tmp_path / "pytest.pyc"
    forbidden.write_bytes(b"sourceless shadow")
    ignored[0] = b"pytest.pyc\0"
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^checkout_ignored_artifact$"):
        evidence._require_neutralized_ignored_files(tmp_path)

    cache_link = tmp_path / ".pytest_cache" / "linked"
    cache_link.symlink_to(forbidden)
    ignored[0] = b".pytest_cache/linked\0"
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^checkout_ignored_artifact$"):
        evidence._require_neutralized_ignored_files(tmp_path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_commit", "0" * 40),
        ("tree_sha256", "1" * 64),
        ("wheel_sha256", "2" * 64),
        ("database_schema", 47),
    ],
)
def test_validator_binds_all_four_exact_release_fields(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    receipt = _payload(_produce(exact_repository, monkeypatch))
    release = receipt["release"]
    assert isinstance(release, dict)
    release[field] = replacement

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^receipt_binding_invalid$"):
        evidence.validate_receipt(
            _canonical(receipt),
            expected_release=exact_repository.identity,
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class=EVIDENCE_CLASS,
            repo_root=exact_repository.root,
        )


def test_validator_reexecutes_and_rejects_failed_to_passed_projection_tamper(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_outcomes = tuple(
        "FAILED" if index == 0 else "PASSED" for index, _ref in enumerate(exact_repository.refs)
    )
    receipt = _payload(_produce(exact_repository, monkeypatch, outcomes=machine_outcomes))
    execution = receipt["execution"]
    proofs = receipt["proofs"]
    assert isinstance(execution, dict)
    assert isinstance(proofs, list)
    failed_projection_sha256 = execution["outcome_projection_sha256"]

    for proof in proofs:
        assert isinstance(proof, dict)
        proof["outcome"] = "PASSED"
    receipt["result"] = "VERIFIED"
    execution["exit_code"] = 0

    for projection_sha256 in (
        failed_projection_sha256,
        evidence._outcome_projection_sha256(  # noqa: SLF001 - adversarial exact projection
            exact_repository.refs,
            ("PASSED",) * len(exact_repository.refs),
        ),
    ):
        execution["outcome_projection_sha256"] = projection_sha256
        with pytest.raises(evidence.ExactReleaseEvidenceError, match="^execution_evidence_mismatch$"):
            evidence.validate_receipt(
                _canonical(receipt),
                expected_release=exact_repository.identity,
                expected_journey_id=JOURNEY_ID,
                expected_evidence_class=EVIDENCE_CLASS,
                repo_root=exact_repository.root,
            )


def test_validator_reruns_receipt_source_from_detached_checkout_after_later_head(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _produce(exact_repository, monkeypatch)
    later = exact_repository.root / "docs/later-validator.txt"
    later.parent.mkdir()
    later.write_text("validator-only later commit\n", encoding="utf-8")
    _git(exact_repository.root, "add", later.relative_to(exact_repository.root).as_posix())
    _git(exact_repository.root, "commit", "-q", "-m", "later validator")
    later_head = _git(exact_repository.root, "rev-parse", "HEAD")
    assert later_head != exact_repository.identity.source_commit
    observed: list[tuple[Path, str, bool]] = []

    def rerun_detached(
        source_root: Path,
        identity: evidence.ReleaseIdentity,
        journey_id: str,
        evidence_class: str,
        *,
        require_running_producer: bool = True,
    ) -> evidence._ExecutionWitness:  # noqa: SLF001 - exact external witness
        assert source_root != exact_repository.root
        assert identity == exact_repository.identity
        assert (journey_id, evidence_class) == (JOURNEY_ID, EVIDENCE_CLASS)
        detached_head = _git(source_root, "rev-parse", "HEAD")
        assert detached_head == exact_repository.identity.source_commit
        assert not (source_root / "docs/later-validator.txt").exists()
        observed.append((source_root, detached_head, require_running_producer))
        outcomes = ("PASSED",) * len(exact_repository.refs)
        return evidence._execution_witness(  # noqa: SLF001 - code-owned test boundary
            outcomes,
            0,
            _collection_sha256(exact_repository.refs),
            evidence._outcome_projection_sha256(exact_repository.refs, outcomes),  # noqa: SLF001
        )

    monkeypatch.setattr(evidence, "_run_closed_pytest", rerun_detached)
    validated = evidence.validate_receipt(
        raw,
        expected_release=exact_repository.identity,
        expected_journey_id=JOURNEY_ID,
        expected_evidence_class=EVIDENCE_CLASS,
        repo_root=exact_repository.root,
    )

    assert validated["result"] == "VERIFIED"
    assert len(observed) == 1
    assert observed[0][1:] == (exact_repository.identity.source_commit, False)
    assert not observed[0][0].exists()


@pytest.mark.parametrize(
    ("tamper", "failure_code"),
    [
        ("producer_hash", "execution_binding_invalid"),
        ("test_hash", "proofs_invalid"),
        ("generic_test", "proofs_invalid"),
    ],
)
def test_validator_rejects_producer_test_and_generic_proof_tamper(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    failure_code: str,
) -> None:
    receipt = _payload(_produce(exact_repository, monkeypatch))
    execution = receipt["execution"]
    proofs = receipt["proofs"]
    assert isinstance(execution, dict)
    assert isinstance(proofs, list) and isinstance(proofs[0], dict)
    if tamper == "producer_hash":
        execution["producer_source_sha256"] = "0" * 64
    elif tamper == "test_hash":
        proofs[0]["test_source_sha256"] = "0" * 64
    else:
        proofs[0]["test_ref"] = (
            "tests/test_immutable_release_operator.py::"
            "test_installed_surface_smoke_uses_one_hermetic_environment_and_cleans_it"
        )

    with pytest.raises(evidence.ExactReleaseEvidenceError, match=f"^{failure_code}$"):
        evidence.validate_receipt(
            _canonical(receipt),
            expected_release=exact_repository.identity,
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class=EVIDENCE_CLASS,
            repo_root=exact_repository.root,
        )


def test_self_declared_v1_receipt_is_not_machine_evidence(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _payload(_produce(exact_repository, monkeypatch))
    receipt["$schema"] = "friday.golden-journey-sanitized-receipt.v1"
    receipt.pop("execution")
    receipt.pop("owner_smoke")

    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^receipt_fields_invalid$"):
        evidence.validate_receipt(
            _canonical(receipt),
            expected_release=exact_repository.identity,
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class=EVIDENCE_CLASS,
            repo_root=exact_repository.root,
        )


def test_owner_smoke_is_optional_but_must_equal_separately_authenticated_binding(
    exact_repository: ExactRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = evidence.AuthenticatedOwnerSmokeBinding(
        schema="friday.owner-telegram-smoke.v1",
        authority="owner.telegram",
        artifact_ref="evidence/owner_smoke/receipt.json",
        artifact_sha256="d" * 64,
    )
    raw = _produce(exact_repository, monkeypatch, owner_smoke=smoke)

    validated = evidence.validate_receipt(
        raw,
        expected_release=exact_repository.identity,
        expected_journey_id=JOURNEY_ID,
        expected_evidence_class=EVIDENCE_CLASS,
        repo_root=exact_repository.root,
        authenticated_owner_smoke=smoke,
    )
    assert validated["owner_smoke"] == smoke.payload()

    for separately_supplied in (
        None,
        evidence.AuthenticatedOwnerSmokeBinding(
            schema=smoke.schema,
            authority=smoke.authority,
            artifact_ref=smoke.artifact_ref,
            artifact_sha256="e" * 64,
        ),
    ):
        with pytest.raises(
            evidence.ExactReleaseEvidenceError,
            match="^owner_smoke_not_authenticated$",
        ):
            evidence.validate_receipt(
                raw,
                expected_release=exact_repository.identity,
                expected_journey_id=JOURNEY_ID,
                expected_evidence_class=EVIDENCE_CLASS,
                repo_root=exact_repository.root,
                authenticated_owner_smoke=separately_supplied,
            )

    no_smoke = _produce(exact_repository, monkeypatch)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^owner_smoke_not_authenticated$"):
        evidence.validate_receipt(
            no_smoke,
            expected_release=exact_repository.identity,
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class=EVIDENCE_CLASS,
            repo_root=exact_repository.root,
            authenticated_owner_smoke=smoke,
        )
