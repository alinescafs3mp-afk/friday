"""Actual pytest/hooks/parent probes inside a test-only outer PID namespace."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tools import document_contour_live_battery as lifecycle
from tools import quality_gate as gate

_SAMPLE = r"""
import os, signal, subprocess, sys, time
from pathlib import Path
import pytest
MODE = os.environ['PHASE_PROBE_MODE']
if MODE == 'controller': time.sleep(30)

@pytest.fixture(autouse=True)
def interval():
    assert not {signal.SIGINT, signal.SIGTERM}.intersection(signal.pthread_sigmask(signal.SIG_BLOCK, ()))
    if MODE == 'setup': time.sleep(30)
    yield
    if MODE == 'teardown': time.sleep(30)

def test_a(request):
    if MODE in {'parallel', 'uncertain'}: time.sleep(30)
    if MODE == 'missing':
        from tools.quality_gate_deadlines import EventWriter
        EventWriter.finish = lambda *args: None
    if MODE == 'duplicate': request.config._friday_deadline_writer.start(request.node.nodeid)
    if MODE == 'crash': os._exit(7)
    if MODE == 'cancel':
        child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], start_new_session=True)
        Path(os.environ['PHASE_PROBE_CHILD']).write_text(str(child.pid))
        os.kill(int(os.environ['PHASE_PROBE_PARENT']), signal.SIGTERM)
        time.sleep(30)

def test_b():
    if MODE in {'parallel', 'uncertain'}: time.sleep(30)
"""

_PROBE = r"""
import dataclasses, json, os, signal, sys, time
from pathlib import Path
source = Path(sys.argv[1]).resolve(strict=True)
configured = os.environ.get('FRIDAY_QUALITY_GATE_INSTALLED_SITE')
if configured is None:
    installed = None
    sys.path.insert(0, str(source))
else:
    claimed = Path(configured)
    if (not configured or configured != configured.strip() or not claimed.is_absolute()
            or not claimed.is_dir() or claimed.resolve(strict=True) != claimed):
        raise RuntimeError('installed wheel runtime is not canonical')
    installed = claimed
    sys.path[:0] = [str(installed), str(source)]
from tools import quality_gate as gate
from tools import quality_gate_deadlines as d
from tools import quality_gate_phase as phase
from tools import quality_gate_process as process
modules = (gate, d, phase, process)
gate_origins = {module.__name__: str(Path(module.__file__).resolve(strict=True)) for module in modules}
if any(not Path(origin).is_relative_to(source) for origin in gate_origins.values()):
    raise RuntimeError('phase_probe_gate_origin_mismatch')
product_origins = {}
if installed is not None:
    if gate._validated_installed_site(os.environ) != installed:
        raise RuntimeError('phase_probe_runtime_origin_mismatch')
    gate._require_installed_wheel_imports(installed)
    product_origins = {name: str(Path(sys.modules[name].__file__).resolve(strict=True))
        for name in gate._WHEEL_NAMESPACES}
    if any(not Path(origin).is_relative_to(installed) for origin in product_origins.values()):
        raise RuntimeError('phase_probe_product_origin_mismatch')
root = Path(sys.argv[2]); mode = sys.argv[3]
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
nodes = ('test_sample.py::test_a', 'test_sample.py::test_b')
second = 'test_second.py::test_c'
cross = mode == 'clean'
limit = 1 if mode in {'parallel', 'setup', 'teardown', 'uncertain'} else 10
sealed = d.seal_plan(run_id='actual-phase-probe', phases=('non-UI','UI') if cross else ('non-UI',),
    nodes=tuple(d.NodeDeadline(node,'non-UI',10) for node in nodes) + ((d.NodeDeadline(second,'UI',10),) if cross else ()),
    cases=(d.CaseDeadline('CASE-AGGREGATE', nodes + ((second,) if cross else ()), limit),
           d.CaseDeadline('CASE-SHARED',(nodes[0],),10)))
ledger = d.ParentDeadlineLedger(sealed)
if mode == 'uncertain':
    real_scope = phase.OwnedCommandScope
    phase.OwnedCommandScope = lambda: real_scope(cleanup_timeout_s=.1)
    signal.pidfd_send_signal = lambda *args: (_ for _ in ()).throw(PermissionError('probe'))
if mode == 'start_cancel':
    real_start = process.OwnedCommandScope.start
    def cancel_after_start(self, *args, **kwargs):
        child = real_start(self, *args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        return child
    process.OwnedCommandScope.start = cancel_after_start
owner = phase.GateProcessRunner()
error = None
started = time.monotonic_ns()
prior = process._subreaper()
with owner:
    try:
        with gate._isolated_test_environment(root, prepare_schema_backups=False, cleanup_allowed=lambda: owner.cleanup_safe) as env:
            env.update(PHASE_PROBE_MODE=mode, PHASE_PROBE_PARENT=str(os.getpid()), PHASE_PROBE_CHILD=str(root/'descendant.pid'))
            for index, label in enumerate(sealed.phases):
                group = root / label; group.mkdir(mode=0o700)
                module = root / ('test_sample.py' if index == 0 else 'test_second.py')
                command = gate._tier_pytest_command(name='probe '+label, python=sys.executable, source=source,
                    environment=env, report=group/'results.xml', collection=group/'collection.json',
                    modules=(str(module),), workers=2 if index == 0 else 1, distribution='load', basetemp=group/'pytest')
                command = dataclasses.replace(command, argv=tuple('--rootdir='+str(root) if a.startswith('--rootdir=') else a for a in command.argv), cwd=root, timeout_s=1 if mode == 'controller' else 10)
                owner(command, ledger=ledger, phase=label, fifo=group/'events.pipe')
                expected = nodes if index == 0 else (second,)
                assert gate._junit_phase_is_clean(group/'results.xml',phase=label,expected_nodeids=expected)
            observed = ledger.complete(time.monotonic_ns(), succeeded=True)
            phase.validate_deadline_evidence(observed, sealed)
    except BaseException as exc:
        error = str(exc)
    fenced = False
    if not owner.cleanup_safe:
        try: owner.check()
        except RuntimeError: fenced = True
result = dict(mode=mode, error=error, elapsed_ns=time.monotonic_ns()-started,
    commands=owner.commands, cleanup_safe=owner.cleanup_safe, fenced=fenced,
    ledger=ledger.evidence(), scratch_homes=len(list(root.glob('friday-quality-home-*'))), restored=process._subreaper()==prior,
    kernel_echild=process._kernel_has_no_children(), gate_source_root=str(source), installed_site=str(installed) if installed else None,
    gate_origins=gate_origins, product_origins=product_origins)
if (root/'descendant.pid').exists(): result['descendant_absent'] = not Path('/proc/'+(root/'descendant.pid').read_text()).exists()
(root/'result.json').write_text(json.dumps(result))
"""


def _probe(tmp_path: Path, mode: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "probe.py").write_text(_PROBE)
    (tmp_path / "test_sample.py").write_text(_SAMPLE)
    (tmp_path / "test_second.py").write_text("def test_c():\n    assert True\n")
    command = [
        "/usr/bin/bwrap",
        "--unshare-pid",
        "--die-with-parent",
        "--new-session",
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--",
        sys.executable,
        "-I",
        "-B",
        str(tmp_path / "probe.py"),
        str(root),
        str(tmp_path),
        mode,
    ]
    with (tmp_path / "private.log").open("wb") as log:
        outcome = lifecycle._run_worker_process(
            command, environment=dict(os.environ), private_log=log, timeout_sec=18, stdout_limit_bytes=16384
        )
    assert outcome.returncode == 0, (
        outcome.stdout.decode("utf-8", errors="replace") + (tmp_path / "private.log").read_text()
    )
    assert outcome.worker_reaped and outcome.process_group_clear and not outcome.cleanup_failure_codes
    return json.loads((tmp_path / "result.json").read_text())


def _actual_wheel_projection(source: Path, destination: Path) -> Path:
    destination.mkdir(mode=0o700)
    candidate_sha = gate._git_output(source, "rev-parse", "HEAD")  # noqa: SLF001
    _wheel, _digest, _comparison, installed, _python = gate._build_reusable_wheel(  # noqa: SLF001
        source,
        destination,
        candidate_sha=candidate_sha,
        python=sys.executable,
        environment={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        runner=gate.run_command,
    )
    assert not (installed / "tools").exists()
    return installed.resolve(strict=True)


def test_fenced_cleanup_removes_owned_sealed_tree_without_following_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "scratch"
    parent.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    marker = outside / "marker"
    marker.write_bytes(b"outside")
    marker.chmod(0o400)

    with gate._fenced_temporary_directory(  # noqa: SLF001 - exact gate harness control
        prefix="owned-", dir=parent, cleanup_allowed=lambda: True
    ) as raw:
        root = Path(raw)
        sealed = root / "sealed"
        nested = sealed / "nested"
        nested.mkdir(parents=True)
        payload = nested / "payload"
        payload.write_bytes(b"sealed")
        payload.chmod(0o400)
        (sealed / "escape").symlink_to(outside, target_is_directory=True)
        nested.chmod(0o500)
        sealed.chmod(0o500)

    assert not root.exists()
    assert marker.read_bytes() == b"outside"
    assert marker.stat().st_mode & 0o777 == 0o400


def test_fenced_cleanup_retains_scratch_until_cleanup_is_confirmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with gate._fenced_temporary_directory(  # noqa: SLF001 - exact gate harness control
        prefix="uncertain-", dir=tmp_path, cleanup_allowed=lambda: False
    ) as raw:
        root = Path(raw)
        sealed = root / "sealed"
        sealed.mkdir()
        sealed.chmod(0o500)

    assert root.is_dir() and sealed.is_dir()
    assert sealed.stat().st_mode & 0o777 == 0o500
    assert "uncertain child cleanup" in capsys.readouterr().err
    gate._remove_owned_scratch_tree(str(root))  # noqa: SLF001 - clean the retained test fixture


def test_fenced_cleanup_refuses_a_different_directory_at_the_original_path(tmp_path: Path) -> None:
    original: Path | None = None
    replacement: Path | None = None
    try:
        with (
            pytest.raises(RuntimeError, match="^quality_gate_scratch_cleanup_refused$"),
            gate._fenced_temporary_directory(  # noqa: SLF001 - exact gate harness control
                prefix="replaced-", dir=tmp_path, cleanup_allowed=lambda: True
            ) as raw,
        ):
            replacement = Path(raw)
            original = tmp_path / f"{replacement.name}-original"
            original_marker = replacement / "original-marker"
            original_marker.write_bytes(b"original")
            original_marker.chmod(0o400)
            replacement.rename(original)
            replacement.mkdir(mode=0o700)
            replacement_marker = replacement / "replacement-marker"
            replacement_marker.write_bytes(b"replacement")
            replacement_marker.chmod(0o400)
            replacement.chmod(0o500)

        assert original is not None and replacement is not None
        assert (original / "original-marker").read_bytes() == b"original"
        assert (replacement / "replacement-marker").read_bytes() == b"replacement"
        assert replacement.stat().st_mode & 0o777 == 0o500
        assert (replacement / "replacement-marker").stat().st_mode & 0o777 == 0o400
    finally:
        for retained in (replacement, original):
            if retained is not None and retained.exists():
                gate._remove_owned_scratch_tree(str(retained))  # noqa: SLF001


def test_fenced_cleanup_preserves_body_error_when_root_identity_is_replaced(tmp_path: Path) -> None:
    original: Path | None = None
    replacement: Path | None = None
    caught: pytest.ExceptionInfo[LookupError] | None = None
    try:
        with (
            pytest.raises(LookupError, match="original-body-error") as caught,
            gate._fenced_temporary_directory(  # noqa: SLF001 - exact gate harness control
                prefix="replaced-body-", dir=tmp_path, cleanup_allowed=lambda: True
            ) as raw,
        ):
            replacement = Path(raw)
            original = tmp_path / f"{replacement.name}-original"
            (replacement / "original-marker").write_bytes(b"original")
            replacement.rename(original)
            replacement.mkdir(mode=0o700)
            marker = replacement / "replacement-marker"
            marker.write_bytes(b"replacement")
            marker.chmod(0o400)
            replacement.chmod(0o500)
            raise LookupError("original-body-error")

        assert caught is not None and original is not None and replacement is not None
        assert (original / "original-marker").read_bytes() == b"original"
        assert (replacement / "replacement-marker").read_bytes() == b"replacement"
        assert replacement.stat().st_mode & 0o777 == 0o500
        assert any("scratch cleanup failed" in note for note in getattr(caught.value, "__notes__", ()))
    finally:
        for retained in (replacement, original):
            if retained is not None and retained.exists():
                gate._remove_owned_scratch_tree(str(retained))  # noqa: SLF001


def test_phase_probe_splits_actual_wheel_product_from_source_gate_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[1]
    installed = _actual_wheel_projection(source, tmp_path / "wheel-projection")
    monkeypatch.setenv("FRIDAY_QUALITY_GATE_INSTALLED_SITE", str(installed))
    probe_root = tmp_path / "probe"
    probe_root.mkdir(mode=0o700)

    result = _probe(probe_root, "clean")

    assert result["error"] is None
    assert Path(result["gate_source_root"]) == source
    assert Path(result["installed_site"]) == installed
    assert all(Path(origin).is_relative_to(source) for origin in result["gate_origins"].values())
    assert all(Path(origin).is_relative_to(installed) for origin in result["product_origins"].values())
    assert set(result["product_origins"]) == {"friday", "friday_host_agent", "friday_package_broker"}
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]


def test_phase_probe_rejects_noncanonical_installed_gate_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[1]
    alias = tmp_path / "installed-alias"
    alias.symlink_to(source, target_is_directory=True)
    monkeypatch.setenv("FRIDAY_QUALITY_GATE_INSTALLED_SITE", str(alias))
    probe_root = tmp_path / "probe"
    probe_root.mkdir(mode=0o700)

    with pytest.raises(AssertionError, match="installed wheel runtime is not canonical"):
        _probe(probe_root, "clean")


def test_real_xdist_hooks_complete_once_and_carry_shared_totals_between_phases(tmp_path):
    result = _probe(tmp_path, "clean")
    assert result["error"] is None
    assert result["scratch_homes"] == 0
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    ledger = result["ledger"]
    assert ledger["credit_eligible"] and ledger["retry_count"] == 0 and ledger["event_count"] == 6
    assert len(ledger["phases"]) == 2
    assert all(node["start_events"] == 1 and node["finish_ns"] is not None for node in ledger["attempts"])
    assert ledger["cases"][0]["used_ns"] == sum(node["duration_ns"] for node in ledger["attempts"])
    assert ledger["cases"][1]["used_ns"] == ledger["attempts"][0]["duration_ns"]


@pytest.mark.parametrize("mode", ["parallel", "setup", "teardown"])
def test_actual_active_case_expiry_includes_setup_teardown_and_parallel_members(tmp_path, mode):
    result = _probe(tmp_path, mode)
    assert "ledger_case_deadline_exceeded" in result["error"]
    assert not result["ledger"]["credit_eligible"] and result["ledger"]["retry_count"] == 0
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    assert result["elapsed_ns"] < 8_000_000_000
    assert result["commands"][0]["cleanup"]["forced_leader"]


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "ledger_phase_events_missing"),
        ("duplicate", "ledger_duplicate_start"),
        ("crash", "gate_command_not_clean"),
    ],
)
def test_missing_duplicate_and_crashed_worker_cannot_mint_first_attempt_credit(tmp_path, mode, reason):
    result = _probe(tmp_path, mode)
    assert reason in result["error"]
    assert not result["ledger"]["credit_eligible"]
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    if mode == "duplicate":
        assert result["ledger"]["retry_count"] == 1
    else:
        assert all(node["start_events"] <= 1 for node in result["ledger"]["attempts"])


def test_catchable_parent_cancellation_reaps_detached_descendant_and_denies_credit(tmp_path):
    result = _probe(tmp_path, "cancel")
    assert result["error"] == "gate_cancelled_15"
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    assert result["descendant_absent"] and not result["ledger"]["credit_eligible"]


def test_uncertain_recursive_cleanup_fences_following_commands(tmp_path):
    result = _probe(tmp_path, "uncertain")
    assert not result["cleanup_safe"] and result["fenced"]
    assert result["scratch_homes"] == 1
    assert not result["kernel_echild"] and not result["restored"]
    assert not result["ledger"]["credit_eligible"]
    assert result["commands"][0]["cleanup"]["failure_codes"]


@pytest.mark.parametrize(
    "mode,reason",
    [("controller", "gate_controller_deadline_exceeded"), ("start_cancel", "gate_cancelled_15")],
)
def test_pre_runtest_controller_ceiling_and_launch_cancellation_have_owned_cleanup(tmp_path, mode, reason):
    result = _probe(tmp_path, mode)
    assert result["error"] == reason
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    assert result["scratch_homes"] == 0
    assert result["ledger"]["event_count"] == 0 and not result["ledger"]["credit_eligible"]
