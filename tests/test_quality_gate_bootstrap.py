"""Early admission/cancellation with real Git and a guarded process tree."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tools import document_contour_live_battery as lifecycle

_STALL = r"""
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(os.environ['BOOT_PROBE_ROOT'])
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], start_new_session=True)
(root/'descendant.pid').write_text(str(child.pid))
if os.environ['BOOT_PROBE_MODE'] == 'overflow':
    os.write(1, b'x' * 32768)
else:
    os.kill(os.getppid(), 15)
time.sleep(30)
"""

_GIT = r"""
import os, sys
from pathlib import Path
root = Path(os.environ['BOOT_PROBE_ROOT']); mode = os.environ['BOOT_PROBE_MODE']
if mode in {'clone', 'uncertain'} and 'clone' in sys.argv:
    Path(sys.argv[-1]).mkdir()
    exec(compile((root/'stall.py').read_bytes(), str(root/'stall.py'), 'exec'))
elif mode == 'git' or (mode == 'inventory' and 'safe.directory=' in ' '.join(sys.argv)):
    exec(compile((root/'stall.py').read_bytes(), str(root/'stall.py'), 'exec'))
else:
    os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])
"""

_PROBE = r"""
import hashlib, importlib.util, json, marshal, os, shutil, signal, struct, subprocess, sys
from pathlib import Path
root=Path(sys.argv[1]); source=Path(sys.argv[2]); mode=sys.argv[3]
repo=root/'origin'; (repo/'tools').mkdir(parents=True); (repo/'tests').mkdir()
files=('quality_gate.py','quality_gate_inventory.py','quality_gate_deadlines.py','quality_gate_process.py','quality_gate_phase.py')
for name in files: shutil.copyfile(source/'tools'/name,repo/'tools'/name)
(repo/'tools/release_1_0_acceptance.py').write_text('MARKER="projected-acceptance"\n')
(repo/'tools/release_1_0_deterministic.py').write_text('MARKER="projected-deterministic"\n')
(repo/'tests/test_fixture.py').write_text('def test_fixture(): pass\n')
(repo/'.gitignore').write_text('__pycache__/\n')
env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1',GIT_NO_REPLACE_OBJECTS='1',
    GIT_AUTHOR_NAME='Fixture',GIT_AUTHOR_EMAIL='fixture@example.invalid',GIT_COMMITTER_NAME='Fixture',GIT_COMMITTER_EMAIL='fixture@example.invalid')
for args in [('init','-q'),('add','.'),('commit','-qm','guarded bootstrap fixture')]:
    subprocess.run(['/usr/bin/git','-C',str(repo),*args],env=env,check=True,capture_output=True,timeout=5)
sha=subprocess.check_output(['/usr/bin/git','-C',str(repo),'rev-parse','HEAD'],env=env,timeout=5).decode().strip()
if mode == 'cache':
    target=repo/'tools/quality_gate_phase.py'; details=target.stat()
    cache=Path(importlib.util.cache_from_source(str(target))); cache.parent.mkdir(exist_ok=True)
    code=compile('raise RuntimeError("poisoned bytecode was executed")',str(target),'exec')
    cache.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,int(details.st_mtime),details.st_size)+marshal.dumps(code))
spec=importlib.util.spec_from_file_location('bootstrap_fixture_gate',repo/'tools/quality_gate.py')
gate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=gate;spec.loader.exec_module(gate)
before=tuple(sys.path); error=None; scratch=None; observed={}
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT,signal.SIGTERM})
os.environ.update(BOOT_PROBE_ROOT=str(root),BOOT_PROBE_MODE=mode)
if mode in {'clone','uncertain','git','inventory'}: gate.GIT=str(root/'git-shim')
with gate._early_process_owner() as owner:
    helper=sys.modules['_friday_gate_bootstrap.quality_gate_process']
    phase=sys.modules['_friday_gate_bootstrap.quality_gate_phase']
    prior=helper._subreaper()
    if mode == 'uncertain':
        real_scope=phase.OwnedCommandScope
        phase.OwnedCommandScope=lambda:real_scope(cleanup_timeout_s=.1)
        signal.pidfd_send_signal=lambda *args:(_ for _ in ()).throw(PermissionError('probe'))
    popen=subprocess.Popen
    if mode == 'launch':
        def signal_before_assignment(*args,**kwargs):
            child=popen(*args,**kwargs); os.kill(os.getpid(),signal.SIGTERM); return child
        subprocess.Popen=signal_before_assignment
    try:
        if mode == 'dirty':
            with (repo/'tools/quality_gate_phase.py').open('a') as f: f.write('\n# changed after bootstrap\n')
        gate._require_candidate_launcher(sha)
        if mode in {'host','overflow'}:
            # _host_command intentionally closes its environment. The fixed
            # probe carries only test-owned paths, not operator selectors.
            code='import os;os.environ.update(BOOT_PROBE_ROOT='+repr(str(root))+',BOOT_PROBE_MODE='+repr(mode)+');exec(compile(open('+repr(str(root/'stall.py'))+',"rb").read(),"stall","exec"))'
            gate._host_command((sys.executable,'-I','-c',code))
        else:
            assert gate._host_command(('/usr/bin/printf','observed'))=='observed'
        with gate._fenced_temporary_directory(prefix='scratch-',dir=root,cleanup_allowed=lambda:owner.cleanup_safe) as raw:
            scratch=Path(raw)
            with gate._candidate_projection(sha,scratch) as projected:
                load=gate._candidate_inventory_loader()
                inventory=sys.modules[load.__module__]
                assert inventory.candidate_test_modules(projected,sha,git_output=gate._inventory_git_output)==('tests/test_fixture.py',)
                a,d=gate._candidate_r10_modules(projected)
                assert a.MARKER=='projected-acceptance' and d.MARKER=='projected-deterministic'
                observed['projected_namespace']=list(sys.modules['tools'].__path__)==[str(projected/'tools')]
        gate._require_candidate_launcher(sha)
    except BaseException as exc: error=str(exc)
    finally: subprocess.Popen=popen
    observed.update(error=error,cleanup_safe=owner.cleanup_safe,commands=owner.commands,
        kernel_echild=helper._kernel_has_no_children(),restored=helper._subreaper()==prior,
        scratch_retained=scratch is not None and scratch.exists(),path_unchanged=tuple(sys.path)==before,
        no_product_imports=not any(n=='friday' or n.startswith('friday.') for n in sys.modules))
observed['bootstrap_unloaded']=gate._ACTIVE_PROCESS_OWNER is None and not gate._BOOTSTRAP_SOURCES and not any(n.startswith('_friday_gate_bootstrap') for n in sys.modules)
if (root/'descendant.pid').exists(): observed['descendant_absent']=not Path('/proc/'+(root/'descendant.pid').read_text()).exists()
(root/'result.json').write_text(json.dumps(observed))
"""


def _probe(tmp_path: Path, mode: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "probe.py").write_text(_PROBE)
    (tmp_path / "stall.py").write_text(_STALL)
    shim = tmp_path / "git-shim"
    shim.write_text(f"#!{sys.executable}\n" + _GIT)
    shim.chmod(0o700)
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
        str(tmp_path),
        str(root),
        mode,
    ]
    with (tmp_path / "private.log").open("wb") as log:
        outcome = lifecycle._run_worker_process(
            command, environment=dict(os.environ), private_log=log, timeout_sec=20, stdout_limit_bytes=32768
        )
    assert outcome.returncode == 0, (tmp_path / "private.log").read_text()
    assert outcome.worker_reaped and outcome.process_group_clear and not outcome.cleanup_failure_codes
    return json.loads((tmp_path / "result.json").read_text())


@pytest.mark.parametrize("mode", ["clean", "cache"])
def test_bootstrap_owns_real_admission_clone_inventory_and_keeps_projected_namespace(tmp_path, mode):
    result = _probe(tmp_path, mode)
    assert result["error"] is None
    assert result["path_unchanged"] and result["no_product_imports"] and result["projected_namespace"]
    assert (
        result["bootstrap_unloaded"]
        and result["cleanup_safe"]
        and result["kernel_echild"]
        and result["restored"]
    )
    assert not result["scratch_retained"]
    assert {row["name"] for row in result["commands"]} == {
        "candidate Git read",
        "private candidate clone",
        "exact-host prerequisite",
    }
    assert all(row["status"] == "passed" and row["cleanup"]["kernel_echild"] for row in result["commands"])


@pytest.mark.parametrize("mode", ["git", "clone", "host", "inventory", "launch"])
def test_early_cancellation_reaps_owned_children_before_any_scratch_removal(tmp_path, mode):
    result = _probe(tmp_path, mode)
    assert result["error"] == "gate_cancelled_15"
    assert (
        result["bootstrap_unloaded"]
        and result["cleanup_safe"]
        and result["kernel_echild"]
        and result["restored"]
    )
    assert not result["scratch_retained"]
    if mode != "launch":
        assert result["descendant_absent"]
    assert result["commands"][-1]["status"] == "failed"


def test_early_uncertain_cleanup_retains_partial_clone_and_denies_quiescence(tmp_path):
    result = _probe(tmp_path, "uncertain")
    assert result["error"] == "gate_cancelled_15"
    assert not result["cleanup_safe"] and not result["kernel_echild"] and not result["restored"]
    assert result["scratch_retained"] and not result["descendant_absent"]
    assert result["commands"][-1]["cleanup"]["failure_codes"]


def test_early_output_limit_stops_and_reaps_the_producer(tmp_path):
    result = _probe(tmp_path, "overflow")
    assert result["error"] == "gate_capture_limit_exceeded"
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    assert result["descendant_absent"]


def test_loaded_bootstrap_must_still_match_the_frozen_candidate(tmp_path):
    result = _probe(tmp_path, "dirty")
    assert result["error"] == "closed tier launcher is not the clean candidate"
    assert result["cleanup_safe"] and result["kernel_echild"] and result["restored"]
    assert not result["scratch_retained"]
