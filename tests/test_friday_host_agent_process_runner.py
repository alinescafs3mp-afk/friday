from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import friday_host_agent.process_runner as runner_module
from friday.host_control.adapters.base import (
    ActionSpec,
    AdapterSpec,
    ExecutableRequirement,
    ExecutionSpec,
    PackageRequirement,
)
from friday.host_control.adapters.jq import MAX_JQ_INPUT_BYTES, JqAdapter
from friday.host_control.contracts import ExecutableAttestation, ExecutionProfile, RiskClass
from friday.host_control.plans import HostActionPlan, create_action_plan
from friday.host_control.plans import WorkspaceGrant as PlanGrant
from friday_host_agent.executable_attestation import attest_executable
from friday_host_agent.process_runner import (
    DirectExecTestBackend,
    ProcessResult,
    ProcessRunner,
    ResourceBudgets,
    RunnerUnavailable,
    SystemdUserBackend,
    WorkspaceGrant,
)


def _root_executable(source: str):
    return attest_executable(
        source,
        allowed_paths=(source,),
        allowed_owner_uids=(0,),
        package_name="coreutils",
        package_version="1.0",
        architecture="amd64",
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=1,
        observed_version="synthetic 1.0",
    )


def _execution_and_plan(
    executable,
    *,
    arguments: tuple[str, ...],
    timeout: int = 2,
    output: int = 1024,
    grants: tuple[PlanGrant, ...] = (),
):
    action = ActionSpec(
        action_id="run",
        capability_id="data.synthetic.run",
        summary="Synthetic closed execution",
        security_id="host.actions.execute",
        risk_class=RiskClass.LOCAL_READONLY,
        execution_profile=ExecutionProfile.CLI_LOCAL_READONLY,
        input_schema_id="synthetic_v1",
        output_parser_id="bounded_text_v1",
        timeout_sec=timeout,
        max_output_bytes=output,
    )
    adapter = AdapterSpec(
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=1,
        summary="Synthetic adapter",
        categories=("data",),
        supported_platforms=("ubuntu",),
        packages=(PackageRequirement("apt", "coreutils"),),
        executable=ExecutableRequirement("synthetic", "coreutils", (executable.canonical_path,)),
        actions=(action,),
    )
    execution = ExecutionSpec(
        executable=executable.canonical_path,
        argv=(executable.canonical_path, *arguments),
        profile=ExecutionProfile.CLI_LOCAL_READONLY,
        timeout_sec=timeout,
        max_output_bytes=output,
    )
    plan = create_action_plan(
        plan_id="plan:runner",
        actor_user_id="actor:one",
        actor_own_id="owner:one",
        conversation_id="conversation:one",
        source_message_id="message:one",
        host_agent_id="host-agent:one",
        idempotency_key="idempotency:runner",
        adapter=adapter,
        action=action,
        normalized_arguments={"literal": list(arguments)},
        executable_attestation=executable,
        workspace_grants=grants,
        now=1_000,
    )
    return execution, plan


def _workspace(tmp_path: Path, *, grants: tuple[PlanGrant, ...] = ()) -> WorkspaceGrant:
    root = tmp_path / "job_0123456789abcdef"
    for child in ("input", "work", "output", "evidence"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return WorkspaceGrant(
        job_id="job_0123456789abcdef",
        actor_own_id="owner:one",
        workspace_root=str(root),
        grants=grants,
    )


def _jq_case(
    workspace_base: Path,
    *,
    job_id: str,
    payload: bytes,
) -> tuple[ExecutableAttestation, ExecutionSpec, HostActionPlan, WorkspaceGrant, Path]:
    workspace_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = workspace_base / job_id
    root.mkdir(mode=0o700)
    for child in ("input", "work", "output", "evidence"):
        (root / child).mkdir(mode=0o700)
    source = root / "input" / "source.json"
    source.write_bytes(payload)
    source.chmod(0o600)
    grant = PlanGrant(
        grant_id="grant_0123456789abcdef",
        actor_own_id="owner:one",
        access="read",
        relative_path="input/source.json",
        identity_sha256=hashlib.sha256(payload).hexdigest(),
    )
    executable = attest_executable(
        "/usr/bin/jq",
        allowed_paths=("/usr/bin/jq",),
        allowed_owner_uids=(0,),
        package_name="jq",
        package_version="test",
        architecture="amd64",
        adapter_id="data.jq",
        adapter_schema_version=1,
        implementation_version=1,
        observed_version="jq-test",
    )
    adapter = JqAdapter()
    normalized = adapter.normalize_arguments(
        "extract_fields",
        {
            "compact": True,
            "fields": ["name"],
            "input_grant": grant.grant_id,
        },
    )
    plan = create_action_plan(
        plan_id=f"plan:{job_id}",
        actor_user_id="actor:one",
        actor_own_id="owner:one",
        conversation_id="conversation:one",
        source_message_id="message:one",
        host_agent_id="host-agent:one",
        idempotency_key=f"idempotency:{job_id}",
        adapter=adapter.spec,
        action=adapter.spec.action("extract_fields"),
        normalized_arguments=normalized,
        executable_attestation=executable,
        workspace_grants=(grant,),
        now=1_000,
    )
    execution = adapter.build_execution(plan, executable)
    workspace = WorkspaceGrant(
        job_id=job_id,
        actor_own_id="owner:one",
        workspace_root=str(root),
        grants=(grant,),
    )
    return executable, execution, plan, workspace, source


def _sealed_base(tmp_path: Path) -> Path:
    selected = tmp_path / "agent-private-sealed-inputs"
    selected.mkdir(mode=0o700)
    return selected


def test_direct_test_backend_treats_shell_metacharacters_as_literal_data(tmp_path) -> None:
    executable = _root_executable("/usr/lib/cargo/bin/coreutils/printf")
    execution, plan = _execution_and_plan(executable, arguments=("%s", "; touch escaped"))
    workspace = _workspace(tmp_path)
    result = ProcessRunner(DirectExecTestBackend(), workspace_base=tmp_path).run(
        job_id=workspace.job_id,
        plan=plan,
        executable=executable,
        execution=execution,
        workspace=workspace,
        budgets=ResourceBudgets(),
    )
    assert result.outcome == "completed"
    assert result.stdout == b"; touch escaped"
    assert not (Path(workspace.workspace_root) / "work" / "escaped").exists()
    assert result.cgroup_identity is None, "the injected test backend must not pretend to be systemd"


def test_output_timeout_and_cancel_are_bounded(tmp_path) -> None:
    yes = _root_executable("/usr/lib/cargo/bin/coreutils/yes")
    execution, plan = _execution_and_plan(yes, arguments=("bounded",), timeout=1, output=1024)
    workspace = _workspace(tmp_path)
    timed = ProcessRunner(DirectExecTestBackend(), workspace_base=tmp_path).run(
        job_id=workspace.job_id,
        plan=plan,
        executable=yes,
        execution=execution,
        workspace=workspace,
        budgets=ResourceBudgets(),
    )
    assert timed.outcome == "timed_out"
    assert timed.output_truncated is True
    assert len(timed.stdout) + len(timed.stderr) == 1024

    sleep = _root_executable("/usr/lib/cargo/bin/coreutils/sleep")
    execution, plan = _execution_and_plan(sleep, arguments=("10",), timeout=10)
    cancellation = threading.Event()
    cancellation.set()
    cancelled = ProcessRunner(DirectExecTestBackend(), workspace_base=tmp_path).run(
        job_id=workspace.job_id,
        plan=plan,
        executable=sleep,
        execution=execution,
        workspace=workspace,
        budgets=ResourceBudgets(),
        cancel_event=cancellation,
    )
    assert cancelled.outcome == "cancelled"
    assert cancelled.effect_boundary_crossed is True


def test_capture_failure_after_start_is_reported_as_unknown(tmp_path, monkeypatch) -> None:
    executable = _root_executable("/usr/lib/cargo/bin/coreutils/printf")
    execution, plan = _execution_and_plan(executable, arguments=("output",))
    workspace = _workspace(tmp_path)

    def fail_capture(_descriptor: int, _size: int) -> bytes:
        raise OSError("synthetic capture disconnect")

    monkeypatch.setattr(runner_module, "_read_pipe", fail_capture)
    result = ProcessRunner(DirectExecTestBackend(), workspace_base=tmp_path).run(
        job_id=workspace.job_id,
        plan=plan,
        executable=executable,
        execution=execution,
        workspace=workspace,
        budgets=ResourceBudgets(),
    )
    assert result.outcome == "unknown"
    assert result.effect_boundary_crossed is True
    assert result.error_code in {"runner_failure_after_start", "termination_unconfirmed"}


def test_swapped_workspace_input_is_rejected_before_backend(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    source = Path(workspace.workspace_root) / "input" / "source.bin"
    source.write_bytes(b"approved")
    grant = PlanGrant(
        grant_id="grant_0123456789abcdef",
        actor_own_id="owner:one",
        access="read",
        relative_path="input/source.bin",
        identity_sha256=hashlib.sha256(b"approved").hexdigest(),
    )
    workspace = WorkspaceGrant(
        job_id=workspace.job_id,
        actor_own_id=workspace.actor_own_id,
        workspace_root=workspace.workspace_root,
        grants=(grant,),
    )
    executable = _root_executable("/usr/lib/cargo/bin/coreutils/printf")
    execution, plan = _execution_and_plan(executable, arguments=("ok",), grants=(grant,))
    source.write_bytes(b"swapped")
    with pytest.raises(ValueError, match="identity changed"):
        ProcessRunner(DirectExecTestBackend(), workspace_base=tmp_path).run(
            job_id=workspace.job_id,
            plan=plan,
            executable=executable,
            execution=execution,
            workspace=workspace,
            budgets=ResourceBudgets(),
        )


def test_final_exec_verifier_rejects_path_inode_and_content_swap(tmp_path: Path) -> None:
    executable = tmp_path / "reviewed-tool"
    replacement = tmp_path / "replacement-tool"
    marker = tmp_path / "executed"
    executable.write_text('#!/bin/sh\nprintf approved > "$1"\n', encoding="utf-8")
    replacement.write_text('#!/bin/sh\nprintf attacker > "$1"\n', encoding="utf-8")
    executable.chmod(0o700)
    replacement.chmod(0o700)
    attestation = attest_executable(
        executable,
        allowed_paths=(executable,),
        allowed_owner_uids=(os.geteuid(),),
        package_name="synthetic-package",
        package_version="1.0",
        architecture="amd64",
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=1,
        observed_version="synthetic 1.0",
    )
    target_argv = (str(executable), str(marker))

    admitted = runner_module.subprocess.run(
        runner_module._verified_exec_argv(attestation, target_argv),  # noqa: SLF001
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        stdin=runner_module.subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert admitted.returncode == 0
    assert marker.read_text(encoding="utf-8") == "approved"
    marker.unlink()

    os.replace(replacement, executable)
    refused = runner_module.subprocess.run(
        runner_module._verified_exec_argv(attestation, target_argv),  # noqa: SLF001
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        stdin=runner_module.subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=3,
    )
    assert refused.returncode == 126
    assert not marker.exists()


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        ("symlink", "could not be sealed safely"),
        ("hardlink", "metadata is unsafe"),
        ("oversize", "metadata is unsafe"),
    ],
)
def test_jq_sealing_rejects_workspace_replacement_after_initial_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    error: str,
) -> None:
    workspace_base = tmp_path / "shared-jobs"
    executable, execution, plan, workspace, source = _jq_case(
        workspace_base,
        job_id="job_0123456789abcdef",
        payload=b'{"name":"approved"}',
    )
    backend = DirectExecTestBackend()
    backend_calls = 0

    def replace_after_validation() -> bool:
        nonlocal backend_calls
        external = tmp_path / "external.json"
        if replacement == "symlink":
            external.write_bytes(b'{"name":"approved"}')
            source.unlink()
            source.symlink_to(external)
        elif replacement == "hardlink":
            external.write_bytes(b'{"name":"approved"}')
            external.chmod(0o600)
            source.unlink()
            os.link(external, source)
        else:
            source.write_bytes(b"x" * (MAX_JQ_INPUT_BYTES + 1))
        return True

    def must_not_run(**_kwargs) -> ProcessResult:
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("unsafe jq input reached the execution backend")

    monkeypatch.setattr(backend, "available", replace_after_validation)
    monkeypatch.setattr(backend, "run", must_not_run)
    sealed_base = _sealed_base(tmp_path)
    with pytest.raises(ValueError, match=error):
        ProcessRunner(
            backend,
            workspace_base=workspace_base,
            sealed_input_base=sealed_base,
        ).run(
            job_id=workspace.job_id,
            plan=plan,
            executable=executable,
            execution=execution,
            workspace=workspace,
            budgets=ResourceBudgets(),
        )
    assert backend_calls == 0
    assert list(sealed_base.iterdir()) == []


def test_jq_launch_reads_only_agent_private_sealed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_base = tmp_path / "shared-jobs"
    executable, execution, plan, workspace, source = _jq_case(
        workspace_base,
        job_id="job_0123456789abcdef",
        payload=b'{"name":"approved"}',
    )
    sealed_base = _sealed_base(tmp_path)
    backend = DirectExecTestBackend()
    direct_run = backend.run
    observed_working_directory: Path | None = None

    def mutate_shared_source_at_launch(**kwargs) -> ProcessResult:
        nonlocal observed_working_directory
        observed_working_directory = kwargs["working_directory"]
        assert observed_working_directory.parent == sealed_base
        assert not observed_working_directory.is_relative_to(workspace_base)
        assert execution.argv[-1] == "source.json"
        assert (observed_working_directory / execution.argv[-1]).read_bytes() == b'{"name":"approved"}'
        assert (observed_working_directory / execution.argv[-1]).stat().st_mode & 0o777 == 0o400
        source.write_bytes(b'{"name":"attacker"}')
        return direct_run(**kwargs)

    monkeypatch.setattr(backend, "run", mutate_shared_source_at_launch)
    result = ProcessRunner(
        backend,
        workspace_base=workspace_base,
        sealed_input_base=sealed_base,
    ).run(
        job_id=workspace.job_id,
        plan=plan,
        executable=executable,
        execution=execution,
        workspace=workspace,
        budgets=ResourceBudgets(),
    )
    assert result.outcome == "completed"
    assert result.stdout == b'{"name":"approved"}\n'
    assert source.read_bytes() == b'{"name":"attacker"}'
    assert observed_working_directory is not None
    assert not observed_working_directory.exists()
    assert list(sealed_base.iterdir()) == []


def test_concurrent_jq_jobs_receive_isolated_ephemeral_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_base = tmp_path / "shared-jobs"
    first = _jq_case(
        workspace_base,
        job_id="job_0123456789abcdef",
        payload=b'{"name":"Ada"}',
    )
    second = _jq_case(
        workspace_base,
        job_id="job_fedcba9876543210",
        payload=b'{"name":"Grace"}',
    )
    sealed_base = _sealed_base(tmp_path)
    backend = DirectExecTestBackend()
    direct_run = backend.run
    barrier = threading.Barrier(2)
    observed: list[Path] = []
    observed_lock = threading.Lock()

    def synchronize_launch(**kwargs) -> ProcessResult:
        with observed_lock:
            observed.append(kwargs["working_directory"])
        barrier.wait(timeout=5)
        return direct_run(**kwargs)

    monkeypatch.setattr(backend, "run", synchronize_launch)
    runner = ProcessRunner(
        backend,
        workspace_base=workspace_base,
        sealed_input_base=sealed_base,
    )

    def execute(case) -> ProcessResult:
        executable, execution, plan, workspace, _source = case
        return runner.run(
            job_id=workspace.job_id,
            plan=plan,
            executable=executable,
            execution=execution,
            workspace=workspace,
            budgets=ResourceBudgets(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, (first, second)))
    assert {item.stdout for item in results} == {b'{"name":"Ada"}\n', b'{"name":"Grace"}\n'}
    assert len(set(observed)) == 2
    assert all(path.parent == sealed_base for path in observed)
    assert list(sealed_base.iterdir()) == []


def test_production_default_fails_closed_without_systemd_boundary(tmp_path) -> None:
    executable = _root_executable("/usr/lib/cargo/bin/coreutils/printf")
    execution, plan = _execution_and_plan(executable, arguments=("ok",))
    workspace = _workspace(tmp_path)
    unavailable = SystemdUserBackend(systemd_run="/missing/systemd-run", systemctl="/missing/systemctl")
    with pytest.raises(RunnerUnavailable, match="unavailable"):
        ProcessRunner(unavailable, workspace_base=tmp_path).run(
            job_id=workspace.job_id,
            plan=plan,
            executable=executable,
            execution=execution,
            workspace=workspace,
            budgets=ResourceBudgets(),
        )


def test_static_boundary_preflight_rejects_missing_bwrap_without_user_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    backend = SystemdUserBackend(systemd_run=trusted_launcher, systemctl=trusted_launcher)
    monkeypatch.setattr(runner_module, "_BWRAP_EXECUTABLE", "/missing/friday-bwrap")

    def forbidden_live_probe() -> bool:
        raise AssertionError("static preflight entered the user manager")

    monkeypatch.setattr(backend, "_probe_effective_boundary", forbidden_live_probe)

    assert backend.static_available() is False


def test_systemd_backend_builds_code_owned_cgroup_and_resource_limits(tmp_path, monkeypatch) -> None:
    executable = _root_executable("/usr/lib/cargo/bin/coreutils/printf")
    execution, _plan = _execution_and_plan(executable, arguments=("ok",))
    workspace = _workspace(tmp_path)
    captured: dict[str, object] = {}
    expected = ProcessResult(
        outcome="completed",
        effect_boundary_crossed=True,
        unit_id="friday-host-0123456789abcdef.service",
        cgroup_identity="systemd-user:friday-host-0123456789abcdef.service",
        exit_code=0,
        signal=None,
        started_at=1.0,
        finished_at=2.0,
        timed_out=False,
        cancelled=False,
        output_truncated=False,
        stdout=b"ok",
        stderr=b"",
    )

    def capture(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return expected

    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    backend = SystemdUserBackend(systemd_run=trusted_launcher, systemctl=trusted_launcher)
    monkeypatch.setattr(runner_module, "_capture_process", capture)
    result = backend.run(
        job_id=workspace.job_id,
        executable=executable,
        execution=execution,
        working_directory=workspace.resolve("job_work"),
        budgets=ResourceBudgets(
            memory_max_bytes=64 * 1024 * 1024,
            tasks_max=8,
            cpu_quota_percent=50,
            file_size_max_bytes=4 * 1024 * 1024,
        ),
        cancel_event=None,
    )
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "--property=MemoryMax=67108864" in argv
    assert "--property=TasksMax=8" in argv
    assert "--property=CPUQuota=50%" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=RestrictAddressFamilies=AF_UNIX AF_NETLINK" in argv
    assert not any("PrivateUsers=" in item or "TemporaryFileSystem=" in item for item in argv)
    assert not any(item.startswith("--setenv=") for item in argv)
    separator = argv.index("--")
    assert argv[separator + 1 : separator + 6] == [
        "/usr/bin/env",
        "--ignore-environment",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
        "PATH=/usr/bin:/bin",
    ]
    bwrap_start = separator + 6
    bwrap_separator = argv.index("--", bwrap_start)
    sandbox = argv[bwrap_start : bwrap_separator + 1]
    assert sandbox[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in sandbox
    assert "--unshare-user" in sandbox
    assert "--share-net" not in sandbox
    assert "--disable-userns" in sandbox
    assert sandbox[3:7] == ["--uid", str(os.geteuid()), "--gid", str(os.getegid())]
    assert sandbox[7:9] == ["--cap-drop", "ALL"]
    assert "--ro-bind" in sandbox
    working_directory = str(workspace.resolve("job_work"))
    assert ["--ro-bind", working_directory, working_directory] == sandbox[-6:-3]
    assert ["--chdir", working_directory, "--"] == sandbox[-3:]
    verified = argv[bwrap_separator + 1 :]
    assert verified[:4] == [
        "/usr/bin/python3",
        "-I",
        "-c",
        runner_module._VERIFIED_EXEC_SCRIPT,  # noqa: SLF001 - exact production boundary
    ]
    assert verified[4] == executable.canonical_path
    assert verified[-len(execution.argv) :] == list(execution.argv)
    assert captured["environment"] == {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.geteuid()}/bus",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{os.geteuid()}",
    }
    assert result.cgroup_identity == "systemd-user:friday-host-0123456789abcdef.service"

    network_execution = ExecutionSpec(
        executable=execution.executable,
        argv=execution.argv,
        profile=ExecutionProfile.CLI_NETWORK_UNPRIVILEGED,
        timeout_sec=execution.timeout_sec,
        max_output_bytes=execution.max_output_bytes,
    )
    backend.run(
        job_id=workspace.job_id,
        executable=executable,
        execution=network_execution,
        working_directory=workspace.resolve("job_work"),
        budgets=ResourceBudgets(),
        cancel_event=None,
    )
    network_argv = captured["argv"]
    assert isinstance(network_argv, list)
    assert "--property=RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in network_argv
    assert "--share-net" in network_argv


def test_systemd_availability_smokes_both_exact_bwrap_profiles_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def successful_probe(argv, **_kwargs):
        calls.append(argv)
        return runner_module.subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    monkeypatch.setattr(runner_module.subprocess, "run", successful_probe)
    backend = SystemdUserBackend(
        systemd_run=trusted_launcher,
        systemctl=trusted_launcher,
        probe_base=tmp_path,
    )
    assert backend.available() is True
    assert backend.available() is True
    assert len(calls) == 2
    assert all("--collect" in item for item in calls)
    assert all("/usr/bin/env" in item and "/usr/bin/bwrap" in item for item in calls)
    assert all(item[-4] == "/usr/bin/test" and item[-3:-1] == ["!", "-e"] for item in calls)
    assert "--share-net" not in calls[0]
    assert "--share-net" in calls[1]
    assert all(any(argument.startswith("--property=MemoryMax=") for argument in item) for item in calls)


def test_systemd_availability_fails_closed_when_effective_bwrap_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def failed_probe(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return runner_module.subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"denied")

    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    monkeypatch.setattr(runner_module.subprocess, "run", failed_probe)
    backend = SystemdUserBackend(
        systemd_run=trusted_launcher,
        systemctl=trusted_launcher,
        probe_base=tmp_path,
    )
    assert backend.available() is False
    assert backend.available() is False
    assert calls == 1


def test_systemd_boundary_probe_negative_control_detects_visible_sibling_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_test = str(Path("/usr/bin/test").resolve(strict=True))
    backend = SystemdUserBackend(
        systemd_run=trusted_test,
        systemctl=trusted_test,
        probe_base=tmp_path,
    )

    def deliberately_leaky_command(**kwargs) -> list[str]:
        return list(kwargs["target_argv"])

    monkeypatch.setattr(backend, "_command", deliberately_leaky_command)
    assert backend.available() is False
    assert list(tmp_path.iterdir()) == []


def test_systemd_reconcile_requires_terminal_unit_and_empty_exact_cgroup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    backend = SystemdUserBackend(systemd_run=trusted_launcher, systemctl=trusted_launcher)
    backend._cgroup_root = tmp_path  # noqa: SLF001 - synthetic cgroup-v2 mount
    cgroup = "/user.slice/friday-host-test.scope"
    events = tmp_path / cgroup.removeprefix("/") / "cgroup.events"
    events.parent.mkdir(parents=True)
    events.write_text("populated 0\nfrozen 0\n", encoding="ascii")

    def show_unit(argv, **_kwargs):
        assert "--property=ControlGroup" in argv
        stdout = (
            "LoadState=loaded\n"
            "ActiveState=inactive\n"
            "SubState=dead\n"
            "Result=signal\n"
            "ExecMainCode=2\n"
            "ExecMainStatus=15\n"
            "MainPID=0\n"
            "ControlPID=0\n"
            f"ControlGroup={cgroup}\n"
        ).encode()
        return runner_module.subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(runner_module.subprocess, "run", show_unit)
    terminal = backend.reconcile("job_0123456789abcdef")
    assert terminal["terminal_observed"] == "true"
    assert terminal["cgroup_populated"] == "0"

    events.write_text("populated 1\nfrozen 0\n", encoding="ascii")
    surviving = backend.reconcile("job_0123456789abcdef")
    assert surviving["terminal_observed"] == "false"
    assert surviving["cgroup_populated"] == "1"


def test_systemd_cancel_escalates_and_never_trusts_kill_return_code_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    backend = SystemdUserBackend(systemd_run=trusted_launcher, systemctl=trusted_launcher)
    signals: list[tuple[str, str]] = []

    monkeypatch.setattr(
        backend,
        "_observe_unit",
        lambda unit: {"unit_id": unit, "state": "active", "terminal_observed": "false"},
    )
    monkeypatch.setattr(backend, "_wait_for_terminal", lambda _unit, *, timeout_sec: False)

    def accepted_signal(argv, **_kwargs):
        signals.append((next(item for item in argv if item.startswith("--signal=")), argv[-1]))
        return runner_module.subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner_module.subprocess, "run", accepted_signal)
    assert backend.cancel("job_0123456789abcdef") is False
    assert signals == [
        ("--signal=SIGTERM", "friday-host-0123456789abcdef.service"),
        ("--signal=SIGKILL", "friday-host-0123456789abcdef.service"),
    ]

    signals.clear()
    assert backend.cancel("hjob_0123456789abcdef") is False
    assert signals == [
        ("--signal=SIGTERM", "friday-host-0123456789abcdef.service"),
        ("--signal=SIGKILL", "friday-host-0123456789abcdef.service"),
    ]


def test_systemd_cancel_reports_success_only_after_polled_terminal_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_launcher = "/usr/lib/cargo/bin/coreutils/true"
    backend = SystemdUserBackend(systemd_run=trusted_launcher, systemctl=trusted_launcher)
    unit = "friday-host-0123456789abcdef.service"
    observations = iter(
        (
            {"unit_id": unit, "state": "active", "terminal_observed": "false"},
            {"unit_id": unit, "state": "active", "terminal_observed": "false"},
            {
                "unit_id": unit,
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "ControlPID": "0",
                "cgroup_populated": "0",
            },
        )
    )
    observed_units: list[str] = []

    def observe(selected: str) -> dict[str, str]:
        observed_units.append(selected)
        return next(observations)

    monkeypatch.setattr(backend, "_observe_unit", observe)
    monkeypatch.setattr(runner_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda argv, **_kwargs: runner_module.subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b""),
    )
    assert backend.cancel("job_0123456789abcdef") is True
    assert observed_units == [unit, unit, unit]
