from __future__ import annotations

import hashlib
import inspect
import json
import os
import py_compile
import signal
import subprocess
import sys
import sysconfig
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

import pytest

from tools import exact_release_evidence as evidence
from tools import quality_gate

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
    ignore = root / ".gitignore"
    producer.parent.mkdir(parents=True)
    test_source.parent.mkdir(parents=True)

    current_producer = Path(evidence.__file__).read_bytes()
    producer.write_bytes(current_producer)
    test_source.write_text(
        f"def {TEST_FUNCTION}():\n    pass\n",
        encoding="utf-8",
    )
    ignore.write_text(".venv/\n", encoding="ascii")

    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Exact Evidence Test")
    _git(root, "config", "user.email", "exact-evidence@example.invalid")
    _git(
        root,
        "add",
        evidence.PRODUCER_PATH,
        test_source.relative_to(root).as_posix(),
        ignore.relative_to(root).as_posix(),
    )
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


def test_native_controller_clones_exact_source_without_ignored_tooling(
    exact_repository: ExactRepository,
    tmp_path: Path,
) -> None:
    ignored = exact_repository.root / ".venv/bin/python"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"ignored mutable interpreter")
    origin, head = evidence._validation_origin_identity(exact_repository.root)  # noqa: SLF001
    scratch = tmp_path / "controller-scratch"
    scratch.mkdir(mode=0o700)

    controller = evidence._private_validation_checkout(origin, head, scratch)  # noqa: SLF001

    assert ignored.is_file()
    assert not (controller / ".venv").exists()
    evidence._require_exact_checkout(controller, head)  # noqa: SLF001


def _bootstrap_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    temporary = root / "tmp"
    home.mkdir()
    temporary.mkdir()
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def _run_bootstrap_producer(
    producer_path: Path,
    arguments: tuple[str, ...],
    *,
    options: tuple[str, ...] = ("-I", "-S", "-B"),
    wrong_interpreter_argument: bool = False,
    wrong_producer_bytes: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    target = Path(sys.executable).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    tooling = Path(sysconfig.get_path("purelib")).resolve(strict=True)
    interpreter = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    expected_sha256 = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    producer = os.open(producer_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    if wrong_producer_bytes:
        producer_path.write_bytes(producer_path.read_bytes() + b"\n# tampered\n")
    environment_root = producer_path.parent / "bootstrap-environment"
    environment_root.mkdir()
    try:
        command = (
            str(target),
            *options,
            "-c",
            evidence._ISOLATED_VALIDATION_BOOTSTRAP,  # noqa: SLF001
            str(producer if wrong_interpreter_argument else interpreter),
            str(producer),
            str(producer_path),
            expected_sha256,
            str(target),
            ".".join(str(part) for part in sys.version_info[:3]),
            str(stdlib),
            str(tooling),
            str(os.getpid()),
            *arguments,
        )
        return evidence._run_validation_controller(  # noqa: SLF001
            command,
            cwd=producer_path.parent,
            environment=_bootstrap_environment(environment_root),
            raw=b"",
            interpreter_descriptor=interpreter,
            producer_descriptor=producer,
        )
    finally:
        os.close(producer)
        os.close(interpreter)


def _write_bootstrap_producer(path: Path, marker: Path) -> None:
    path.write_text(
        "from dataclasses import dataclass\n"
        "from pathlib import Path\n"
        "import sys\n"
        "@dataclass(frozen=True)\n"
        "class Bound:\n"
        "    value: str\n"
        "def main(argv):\n"
        "    assert __name__ in sys.modules\n"
        "    Path(argv[0]).write_text(Bound('registered').value, encoding='ascii')\n"
        "    return 0\n",
        encoding="ascii",
    )
    assert not marker.exists()


def test_bootstrap_executes_registered_module_from_exact_producer_fd(tmp_path: Path) -> None:
    marker = tmp_path / "registered"
    producer = tmp_path / "producer.py"
    _write_bootstrap_producer(producer, marker)

    completed = _run_bootstrap_producer(producer, (str(marker),))

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert marker.read_text(encoding="ascii") == "registered"


@pytest.mark.parametrize("options", [("-S", "-B"), ("-I", "-B"), ("-I", "-S")])
def test_bootstrap_rejects_non_isolated_startup_before_producer_exec(
    tmp_path: Path,
    options: tuple[str, ...],
) -> None:
    marker = tmp_path / "not-executed"
    producer = tmp_path / "producer.py"
    _write_bootstrap_producer(producer, marker)

    completed = _run_bootstrap_producer(producer, (str(marker),), options=options)

    assert completed.returncode != 0
    assert completed.stderr
    assert not marker.exists()


@pytest.mark.parametrize("tamper", ["interpreter_fd", "producer_bytes"])
def test_bootstrap_rejects_wrong_fd_or_producer_bytes_before_exec(
    tmp_path: Path,
    tamper: str,
) -> None:
    marker = tmp_path / "not-executed"
    producer = tmp_path / "producer.py"
    _write_bootstrap_producer(producer, marker)

    completed = _run_bootstrap_producer(
        producer,
        (str(marker),),
        wrong_interpreter_argument=tamper == "interpreter_fd",
        wrong_producer_bytes=tamper == "producer_bytes",
    )

    assert completed.returncode != 0
    assert completed.stderr
    assert not marker.exists()


def test_native_interpreter_binding_is_finite_physical_and_nofollow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[int] = []
    original_open = evidence.os.open

    def open_bound(*args: Any, **kwargs: Any) -> int:
        opened.append(int(args[1]))
        return original_open(*args, **kwargs)

    monkeypatch.setattr(evidence.os, "open", open_bound)
    monkeypatch.setattr(
        evidence.os,
        "scandir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recursive runtime scan")),
    )
    descriptor, target, opened_status, directories, stdlib, python_zip = (
        evidence._open_validation_interpreter()  # noqa: SLF001
    )
    try:
        assert evidence._same_validation_status(opened_status, os.fstat(descriptor))  # noqa: SLF001
        assert stdlib in {path for path, _status in directories}
        assert python_zip[0].name == f"python{sys.version_info.major}{sys.version_info.minor}.zip"
        assert len(directories) < 16
        assert opened and all(flags & os.O_NOFOLLOW for flags in opened)
    finally:
        os.close(descriptor)


def test_runtime_anchor_allows_writable_toolcache_outer_but_not_runtime(
    tmp_path: Path,
) -> None:
    toolcache = tmp_path / "hostedtoolcache"
    anchor = toolcache / "Python/3.14.4/x64"
    target = anchor / "bin/python"
    stdlib = anchor / "lib/python3.14"
    (stdlib / "lib-dynload").mkdir(parents=True)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"physical interpreter placeholder")
    for protected in (anchor, target.parent, anchor / "lib", stdlib, stdlib / "lib-dynload"):
        protected.chmod(0o755)
    toolcache.chmod(0o777)

    bindings = evidence._validation_runtime_directories(target, stdlib)  # noqa: SLF001

    assert toolcache in {path for path, _status in bindings}
    anchor.chmod(0o775)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^validation_controller_invalid$"):
        evidence._validation_runtime_directories(target, stdlib)  # noqa: SLF001


def test_native_controller_rejects_non_clean_scope_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_run_validation_controller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^native_validation_scope_invalid$"):
        evidence.validate_receipt_via_native_controller(
            b"",
            expected_release=evidence.ReleaseIdentity("0" * 40, "1" * 64, "2" * 64, 50),
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class=EVIDENCE_CLASS,
            repo_root=tmp_path,
        )


@pytest.mark.parametrize("tamper", ["child_marker", "sigchld"])
def test_native_controller_rejects_inherited_process_state_before_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    if tamper == "child_marker":
        monkeypatch.setattr(evidence, "_NATIVE_VALIDATION_TOOLING_SITE", tmp_path)
    else:
        monkeypatch.setattr(evidence.signal, "getsignal", lambda _signal: signal.SIG_IGN)
    monkeypatch.setattr(
        evidence,
        "_validation_origin_identity",
        lambda *_args: (_ for _ in ()).throw(AssertionError("origin inspected")),
    )
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^validation_controller_invalid$"):
        evidence.validate_receipt_via_native_controller(
            b"",
            expected_release=evidence.ReleaseIdentity("0" * 40, "1" * 64, "2" * 64, 50),
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class="clean artifact path",
            repo_root=tmp_path,
            release_root=tmp_path,
        )


def test_direct_runner_rejects_inherited_sigchld_ignore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence.signal, "getsignal", lambda _signal: signal.SIG_IGN)
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^child_process_authority_invalid$"):
        evidence._run_closed_pytest(  # noqa: SLF001
            tmp_path,
            evidence.ReleaseIdentity("0" * 40, "1" * 64, "2" * 64, 50),
            JOURNEY_ID,
            EVIDENCE_CLASS,
        )


def test_clean_direct_validator_requires_fd_bootstrap_marker(tmp_path: Path) -> None:
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^native_validation_context_invalid$"):
        evidence.validate_receipt(
            b"",
            expected_release=evidence.ReleaseIdentity("0" * 40, "1" * 64, "2" * 64, 50),
            expected_journey_id=JOURNEY_ID,
            expected_evidence_class="clean artifact path",
            repo_root=tmp_path,
            release_root=tmp_path,
        )


def _run_validation_process_fixture(code: str, marker: Path) -> subprocess.CompletedProcess[bytes]:
    target = Path(sys.executable).resolve(strict=True)
    interpreter = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    producer = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    try:
        return evidence._run_validation_controller(  # noqa: SLF001
            (str(target), "-I", "-S", "-B", "-c", code, str(marker)),
            cwd=marker.parent,
            environment={"LANG": "C.UTF-8", "PATH": os.defpath, "TMPDIR": str(marker.parent)},
            raw=b"",
            interpreter_descriptor=interpreter,
            producer_descriptor=producer,
        )
    finally:
        os.close(producer)
        os.close(interpreter)


def test_native_controller_retires_residual_process_group_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "descendant.pid"
    monkeypatch.setattr(evidence, "VALIDATION_TERMINATION_GRACE_SECONDS", 0.1)
    code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen((sys.executable,'-I','-S','-B','-c',"
        "'import os,signal,time; signal.signal(signal.SIGTERM,lambda *_:os.setsid()); time.sleep(60)'),"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')"
    )

    completed = _run_validation_process_fixture(code, marker)

    assert completed.returncode == 0
    descendant = Path(f"/proc/{int(marker.read_text(encoding='ascii'))}")
    deadline = time.monotonic() + 5
    while descendant.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not descendant.exists()


def test_native_controller_timeout_kills_and_reaps_its_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "controller.pid"
    monkeypatch.setattr(evidence, "VALIDATION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(evidence, "VALIDATION_TERMINATION_GRACE_SECONDS", 0.1)
    code = (
        "import os,pathlib,signal,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()),encoding='ascii'); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_validation_process_fixture(code, marker)

    assert not Path(f"/proc/{int(marker.read_text(encoding='ascii'))}").exists()


def test_native_controller_dies_with_a_sigkilled_gate_parent(tmp_path: Path) -> None:
    marker = tmp_path / "controller.pid"
    finalizer_marker = tmp_path / "controller.finalizer"
    producer_path = tmp_path / "controller.py"
    producer_path.write_text(
        "import os,time\n"
        "from pathlib import Path\n"
        "class Linger:\n"
        "    def __init__(self,path): self.path=path\n"
        "    def __del__(self,Path=Path,sleep=time.sleep):\n"
        "        Path(self.path).write_text('entered',encoding='ascii')\n"
        "        sleep(60)\n"
        "holder=None\n"
        "def main(argv):\n"
        "    global holder\n"
        "    Path(argv[0]).write_text(str(os.getpid()),encoding='ascii')\n"
        "    holder=Linger(argv[1])\n"
        "    return 0\n",
        encoding="ascii",
    )
    environment_root = tmp_path / "parent-environment"
    (environment_root / "home").mkdir(parents=True)
    (environment_root / "tmp").mkdir()
    helper = (
        "import hashlib,os,sys,sysconfig; "
        "sys.path.insert(0,sys.argv[1]); from tools import exact_release_evidence as e; "
        "producer_path=sys.argv[2]; marker=sys.argv[3]; finalizer_marker=sys.argv[4]; envroot=sys.argv[5]; "
        "raw=open(producer_path,'rb').read(); target=os.path.realpath(sys.executable); "
        "stdlib=os.path.realpath(sysconfig.get_path('stdlib')); "
        "tooling=os.path.realpath(sysconfig.get_path('purelib')); "
        "interpreter=os.open(target,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW); "
        "producer=os.open(producer_path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW); "
        "command=(target,'-I','-S','-B','-c',e._ISOLATED_VALIDATION_BOOTSTRAP,"
        "str(interpreter),str(producer),producer_path,hashlib.sha256(raw).hexdigest(),target,"
        "'.'.join(str(part) for part in sys.version_info[:3]),stdlib,tooling,str(os.getpid()),marker,finalizer_marker); "
        "environment={'HOME':envroot+'/home','LANG':'C.UTF-8','LC_ALL':'C.UTF-8',"
        "'PATH':os.defpath,'TMPDIR':envroot+'/tmp','TZ':'UTC'}; "
        "completed=e._run_validation_controller(command,cwd=os.path.dirname(producer_path),"
        "environment=environment,raw=b'',interpreter_descriptor=interpreter,"
        "producer_descriptor=producer); raise SystemExit(completed.returncode)"
    )
    parent = subprocess.Popen(  # noqa: S603 - exact local helper source
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            helper,
            str(Path(__file__).resolve().parents[1]),
            str(producer_path),
            str(marker),
            str(finalizer_marker),
            str(environment_root),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    controller_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not finalizer_marker.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not finalizer_marker.exists():
            _stdout, stderr = parent.communicate(timeout=5)
            pytest.fail(stderr.decode(errors="replace"))
        controller_pid = int(marker.read_text(encoding="ascii"))
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while Path(f"/proc/{controller_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not Path(f"/proc/{controller_pid}").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if controller_pid is not None and Path(f"/proc/{controller_pid}").exists():
            with suppress(ProcessLookupError):
                os.kill(controller_pid, signal.SIGKILL)


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
        require_isolated_startup: bool = True,
    ) -> evidence._ExecutionWitness:  # noqa: SLF001 - exact internal producer witness
        assert repo_root == repository.root
        assert identity == repository.identity
        assert (journey_id, evidence_class) == (JOURNEY_ID, EVIDENCE_CLASS)
        assert require_running_producer is True
        assert type(require_isolated_startup) is bool
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

    assert evidence._pytest_outcomes(report, nodeids, gate=quality_gate) == expected


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
        evidence._pytest_outcomes(report, expected, gate=quality_gate)


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

    monkeypatch.setattr(evidence, "_authenticated_quality_gate", lambda **_kwargs: quality_gate)
    monkeypatch.setattr(quality_gate, "_isolated_test_environment", isolated_environment)
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


def test_ignored_checkout_scan_rejects_forged_valid_helper_bytecode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = (
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/CACHEDIR.TAG",
        ".mypy_cache/3.14/cache.db",
    )
    for path_text in allowed:
        path = tmp_path / path_text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cache")

    ignored = [b"\0".join(path.encode() for path in allowed) + b"\0"]
    monkeypatch.setattr(evidence, "_git", lambda *_arguments: ignored[0])
    evidence._require_neutralized_ignored_files(tmp_path)

    helper = tmp_path / "tools/quality_gate.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("raise RuntimeError('forged helper executed')\n", encoding="ascii")
    forbidden = tmp_path / "tools/__pycache__/quality_gate.cpython-314.pyc"
    forbidden.parent.mkdir()
    py_compile.compile(str(helper), cfile=str(forbidden), doraise=True)
    ignored[0] = b"tools/__pycache__/quality_gate.cpython-314.pyc\0"
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^checkout_ignored_artifact$"):
        evidence._require_neutralized_ignored_files(tmp_path)

    cache_link = tmp_path / ".pytest_cache" / "linked"
    cache_link.symlink_to(forbidden)
    ignored[0] = b".pytest_cache/linked\0"
    with pytest.raises(evidence.ExactReleaseEvidenceError, match="^checkout_ignored_artifact$"):
        evidence._require_neutralized_ignored_files(tmp_path)


def test_producer_helper_executes_tracked_blob_only_after_stdlib_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "tools.s6_exact_helper_probe"
    relative_path = "tools/s6_exact_helper_probe.py"
    source = tmp_path / relative_path
    source.parent.mkdir()
    tracked = b"VALUE = 'tracked'\n"
    source.write_bytes(tracked)
    marker = tmp_path / "forged-executed"
    forged_source = tmp_path / "forged.py"
    forged_source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('forged')\n",
        encoding="utf-8",
    )
    forged_pyc = source.parent / "__pycache__/s6_exact_helper_probe.cpython-314.pyc"
    forged_pyc.parent.mkdir()
    py_compile.compile(str(forged_source), cfile=str(forged_pyc), doraise=True)
    events: list[str] = []
    commit = "a" * 40

    def preflight(*, require_isolated_startup: bool = True) -> tuple[Path, str]:
        assert require_isolated_startup is True
        events.append("preflight")
        return tmp_path, commit

    monkeypatch.setattr(evidence, "_running_exact_checkout", preflight)

    def tracked_blob(*_args: object) -> bytes:
        events.append("tracked_blob")
        return tracked

    monkeypatch.setattr(evidence, "_exact_git_blob", tracked_blob)
    monkeypatch.setattr(
        evidence,
        "_require_exact_checkout",
        lambda *_args: events.append("post_checkout"),
    )
    monkeypatch.setattr(
        evidence,
        "_require_running_producer",
        lambda *_args: events.append("post_producer"),
    )
    evidence._AUTHENTICATED_PRODUCER_HELPERS.pop(module_name, None)  # noqa: SLF001
    sys.modules.pop(module_name, None)
    try:
        module = evidence._authenticated_producer_helper(module_name, relative_path)  # noqa: SLF001
        assert module.VALUE == "tracked"
        assert events == ["preflight", "tracked_blob", "post_checkout", "post_producer"]
        assert not marker.exists()
    finally:
        evidence._AUTHENTICATED_PRODUCER_HELPERS.pop(module_name, None)  # noqa: SLF001
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize(
    ("options", "startup_environment"),
    (
        (("-I", "-S"), {}),
        (("-I", "-B"), {}),
        (("-S", "-B"), {}),
        (("-I", "-S", "-B"), {"PYTHONSTARTUP": "/private/producer-startup.py"}),
    ),
)
def test_producer_authority_rejects_unsealed_startup_before_helper_authentication(
    options: tuple[str, ...],
    startup_environment: dict[str, str],
) -> None:
    producer = Path(evidence.__file__).resolve(strict=True)
    probe = (
        "import runpy,sys;"
        "namespace=runpy.run_path(sys.argv[1]);"
        "namespace['_require_producer_process_authority']();"
        "print('AUTHORIZED')"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if (
            name not in evidence._FORBIDDEN_PRODUCER_STARTUP_ENVIRONMENT  # noqa: SLF001
            and not name.startswith(("PYTHON", "LD_", "DYLD_", "GLIBC_"))
        )
    }
    environment.update(startup_environment)

    completed = subprocess.run(
        (sys.executable, *options, "-c", probe, str(producer)),
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )

    assert completed.returncode != 0
    assert b"producer_process_authority_invalid" in completed.stderr


def test_producer_authority_accepts_isolated_stdlib_only_startup() -> None:
    producer = Path(evidence.__file__).resolve(strict=True)
    probe = (
        "import runpy,sys;"
        "namespace=runpy.run_path(sys.argv[1]);"
        "namespace['_require_producer_process_authority']();"
        "print('AUTHORIZED')"
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if (
            name not in evidence._FORBIDDEN_PRODUCER_STARTUP_ENVIRONMENT  # noqa: SLF001
            and not name.startswith(("PYTHON", "LD_", "DYLD_", "GLIBC_"))
        )
    }

    completed = subprocess.run(
        (sys.executable, "-I", "-S", "-B", "-c", probe, str(producer)),
        check=False,
        capture_output=True,
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == b"AUTHORIZED\n"


def test_git_authority_ignores_a_forged_ambient_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    forged_bin = tmp_path / "forged-bin"
    forged_bin.mkdir()
    marker = tmp_path / "forged-git-executed"
    forged_git = forged_bin / "git"
    forged_git.write_text(
        f"#!/bin/sh\nprintf executed > '{marker}'\nprintf '/forged/repository\\n'\n",
        encoding="ascii",
    )
    forged_git.chmod(0o700)
    monkeypatch.setenv("PATH", str(forged_bin))

    observed = evidence._git(repository, "rev-parse", "--show-toplevel")  # noqa: SLF001

    assert observed == f"{repository}\n".encode()
    assert not marker.exists()


def test_git_authority_ignores_ambient_repository_and_config_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    alternate = tmp_path / "alternate"
    repository.mkdir()
    alternate.mkdir()
    _git(repository, "init", "-q")
    _git(alternate, "init", "-q")
    fake_global = tmp_path / "forged-gitconfig"
    fake_global.write_text("[core]\nworktree = /forged/worktree\n", encoding="ascii")
    monkeypatch.setenv("GIT_DIR", str(alternate / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(alternate))
    monkeypatch.setenv("GIT_COMMON_DIR", str(alternate / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(alternate / ".git/index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(alternate / ".git/objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(alternate / ".git/objects"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_global))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/forged/worktree")

    observed = evidence._git(repository, "rev-parse", "--show-toplevel")  # noqa: SLF001

    assert observed == f"{repository}\n".encode()


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
        require_isolated_startup: bool = True,
    ) -> evidence._ExecutionWitness:  # noqa: SLF001 - exact external witness
        assert source_root != exact_repository.root
        assert identity == exact_repository.identity
        assert (journey_id, evidence_class) == (JOURNEY_ID, EVIDENCE_CLASS)
        assert require_isolated_startup is False
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
