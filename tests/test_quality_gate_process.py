"""Real process ownership probes, each guarded by a test-only PID namespace."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tools import document_contour_live_battery as lifecycle

_TREE = r"""
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
root = Path(sys.argv[1]); level = int(sys.argv[2]); mode = sys.argv[3]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
threads = [threading.Thread(target=threading.Event().wait) for _ in range(2)]
for thread in threads: thread.start()
if level < 2:
    subprocess.Popen([sys.executable, '-I', '-B', __file__, str(root), str(level+1), mode], start_new_session=True)
data = {'pid': os.getpid(), 'pgid': os.getpgrp(), 'tids': [t.native_id for t in threads]}
temporary = root / f'node-{level}.tmp'; temporary.write_text(json.dumps(data))
temporary.rename(root / f'node-{level}.json')
if level == 0 and mode == 'orphan':
    while not (root / 'node-2.json').exists(): time.sleep(.01)
    os._exit(0)
threading.Event().wait(30)
os._exit(9)
"""

_PROBE = r"""
import dataclasses, json, os, signal, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools import quality_gate_process as owned
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
root = Path(sys.argv[2]); mode = sys.argv[3]
environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'OWNED_VALUE': 'observed'}
before = {name: os.readlink('/proc/self/ns/' + name) for name in ['pid', 'user', 'mnt', 'net']}
prior = owned._subreaper()
scope = owned.OwnedCommandScope(cleanup_timeout_s=2)
result = {}
if mode in {'existing', 'late_existing'}:
    if mode == 'late_existing': scope.__enter__()
    sentinel = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])
    try:
        try:
            if mode == 'existing': scope.__enter__()
            else: scope.start([sys.executable, '-c', 'pass'], cwd=root, environment=environment)
        except owned.ProcessScopeError as exc: result['admission'] = str(exc)
        else: raise AssertionError('foreign child was accepted')
        if mode == 'late_existing':
            proof = scope.close()
            assert not proof.cleanup_clear and not proof.forced_descendants
        result['sentinel_alive'] = sentinel.poll() is None
    finally:
        sentinel.kill(); sentinel.wait(timeout=3)
        if mode == 'late_existing': owned._subreaper(prior)
else:
    caught = None
    if mode == 'cancel':
        def interrupt(*args): raise KeyboardInterrupt
        old_handler = signal.signal(signal.SIGTERM, interrupt)
    try:
        with scope:
            if mode == 'missing':
                scope.start(['/no-such-owned-command'], cwd=root, environment=environment)
            elif mode in {'clean', 'midphase'}:
                code = "import os,pathlib,subprocess,sys,threading; assert os.environ['OWNED_VALUE']=='observed'; pathlib.Path('mode-proof').write_text('own'); subprocess.run([sys.executable,'-I','-c','pass'],check=True); t=threading.Thread(target=lambda:None); t.start(); t.join()"
                if mode == 'midphase':
                    code = '''import os, subprocess, sys, time
p = subprocess.Popen([sys.executable, '-I', '-c', 'import os,time; child=os.fork(); time.sleep(.05) if child == 0 else None; os._exit(0)'], start_new_session=True)
assert p.wait(timeout=3) == 0
deadline = time.monotonic() + 3
while True:
    try: os.killpg(p.pid, 0)
    except ProcessLookupError: break
    if time.monotonic() >= deadline: raise RuntimeError('adopted zombie blocks nested cleanup')
    time.sleep(.01)
'''
                p = scope.start([sys.executable, '-I', '-B', '-c', code], cwd=root, environment=environment, child_umask=0o077)
                assert scope.wait(timeout=5) == 0
            else:
                p = scope.start([sys.executable, '-I', '-B', str(root / 'tree.py'), str(root), '0', mode], cwd=root, environment=environment)
                deadline = time.monotonic() + 5
                while not (root / 'node-2.json').exists():
                    assert time.monotonic() < deadline
                    time.sleep(.01)
                if mode == 'orphan': assert p.wait(timeout=3) == 0
                if mode == 'timeout': scope.wait(timeout=.05)
                if mode == 'cancel': os.kill(os.getpid(), signal.SIGTERM)
                if mode == 'error': raise ValueError('probe-cancel')
                if mode == 'foreign_pid':
                    census = owned._direct_children
                    owned._direct_children = lambda: census() | {os.getpid()}
                    sender = signal.pidfd_send_signal
                    result['signal_targets'] = []
                    def checked_send(fd, sig):
                        info = Path(f'/proc/self/fdinfo/{fd}').read_text()
                        target = int(next(line.split(':')[1] for line in info.splitlines() if line.startswith('Pid:')))
                        assert target != os.getpid()
                        result['signal_targets'].append(target)
                        sender(fd, sig)
                    signal.pidfd_send_signal = checked_send
                if mode == 'uncertain':
                    def denied(*args): raise PermissionError('injected signal denial')
                    signal.pidfd_send_signal = denied
    except (KeyboardInterrupt, ValueError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        caught = type(exc).__name__
    finally:
        if mode == 'cancel': signal.signal(signal.SIGTERM, old_handler)
    proof = scope.proof
    assert proof is not None and scope.close() is proof
    result.update(proof=dataclasses.asdict(proof), clean_exit=proof.clean_exit, cleanup_clear=proof.cleanup_clear, caught=caught)
    if mode == 'clean': result['child_mode'] = (root / 'mode-proof').stat().st_mode & 0o777
    observations = [json.loads(p.read_text()) for p in sorted(root.glob('node-*.json'))]
    result['observations'] = observations
    result['observed_tasks_absent'] = all(not Path('/proc/' + str(pid)).exists() for row in observations for pid in [row['pid'], *row['tids']])
    result['flag_retained'] = owned._subreaper() == 1
result['kernel_echild'] = owned._kernel_has_no_children()
result['flag_restored'] = owned._subreaper() == prior
result['namespaces_unchanged'] = before == {name: os.readlink('/proc/self/ns/' + name) for name in before}
print(json.dumps(result), flush=True)
# On injected uncertainty the outer test-only namespace is the final guard;
# the probe's failed cleanup proof must remain false, never repaired into PASS.
"""


def _probe(tmp_path: Path, mode: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "tree.py").write_text(_TREE)
    (tmp_path / "probe.py").write_text(_PROBE)
    command = [
        "/usr/bin/bwrap",
        "--unshare-pid",
        "--die-with-parent",
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
            command,
            environment=dict(os.environ),
            private_log=log,
            timeout_sec=12,
            stdout_limit_bytes=8192,
        )
    assert outcome.returncode == 0, (tmp_path / "private.log").read_text()
    assert outcome.worker_reaped and outcome.process_group_clear
    assert not outcome.cleanup_failure_codes
    result = json.loads(outcome.stdout)
    assert result["namespaces_unchanged"]
    return result


def test_clean_exit_preserves_environment_umask_and_host_namespaces(tmp_path):
    result = _probe(tmp_path, "clean")
    assert result["clean_exit"] and result["cleanup_clear"]
    assert result["flag_restored"] and result["kernel_echild"]
    assert result["child_mode"] == 0o600
    assert result["proof"]["leader_returncode"] == 0


def test_adopted_zombies_are_reaped_before_nested_owner_finishes(tmp_path):
    result = _probe(tmp_path, "midphase")
    assert result["clean_exit"] and result["cleanup_clear"]
    assert result["proof"]["reaped_descendants"] == 1
    assert result["flag_restored"] and result["kernel_echild"]


@pytest.mark.parametrize(
    "mode,caught",
    [
        ("timeout", "TimeoutExpired"),
        ("cancel", "KeyboardInterrupt"),
        ("error", "ValueError"),
        ("foreign_pid", None),
    ],
)
def test_interruption_reaps_detached_descendants_and_threads(tmp_path, mode, caught):
    result = _probe(tmp_path, mode)
    assert result["caught"] == caught
    assert result["cleanup_clear"] and not result["clean_exit"]
    assert result["proof"]["forced_leader"] and result["proof"]["forced_descendants"]
    assert result["proof"]["leader_returncode"] == -9
    assert result["proof"]["reaped_descendants"] == 2
    assert len(result["observations"]) == 3
    assert len({row["pgid"] for row in result["observations"]}) == 3
    assert all(len(row["tids"]) == 2 for row in result["observations"])
    assert result["observed_tasks_absent"] and result["kernel_echild"] and result["flag_restored"]
    if mode == "foreign_pid":
        assert set(result["signal_targets"]) == {row["pid"] for row in result["observations"]}


def test_zero_exit_with_live_orphans_cannot_claim_clean_success(tmp_path):
    result = _probe(tmp_path, "orphan")
    assert result["cleanup_clear"] and not result["clean_exit"]
    assert result["proof"]["leader_returncode"] == 0
    assert not result["proof"]["forced_leader"] and result["proof"]["forced_descendants"]
    assert result["proof"]["reaped_descendants"] == 2
    assert result["observed_tasks_absent"] and result["kernel_echild"] and result["flag_restored"]


@pytest.mark.parametrize("mode", ["existing", "late_existing"])
def test_admission_failure_never_signals_foreign_children(tmp_path, mode):
    result = _probe(tmp_path, mode)
    assert result["admission"] == "gate_command_scope_not_exclusive"
    assert result["sentinel_alive"] and result["kernel_echild"] and result["flag_restored"]


def test_start_failure_restores_subreaper_without_fabricating_command_success(tmp_path):
    result = _probe(tmp_path, "missing")
    assert result["caught"] == "FileNotFoundError"
    assert result["cleanup_clear"] and not result["clean_exit"]
    assert not result["proof"]["leader_reaped"]
    assert result["flag_restored"] and result["kernel_echild"]


def test_uncertain_cleanup_preserves_adoption_and_failed_proof(tmp_path):
    result = _probe(tmp_path, "uncertain")
    assert not result["cleanup_clear"] and not result["clean_exit"]
    assert not result["kernel_echild"] and not result["observed_tasks_absent"]
    assert result["flag_retained"] and not result["flag_restored"]
    assert result["proof"]["failure_codes"] == ["gate_recursive_cleanup_failed"]
