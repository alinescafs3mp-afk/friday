"""Isolated universal Engineer command kernel — PLAN-002 / REVIEW-004."""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from friday.organs.engineer.command import (
    CommandError,
    CommandGrantAuthority,
    CommandKernel,
    CommandLane,
    CommandOrigin,
    CommandRequest,
    CommandStatus,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSource,
    OwnerSourceAuthority,
    ResourceLimits,
    TrustedPathContract,
)
from friday.organs.engineer.command.boundary import MissingControllerBoundary, SystemdCgroupBoundary
from friday.organs.engineer.command.contracts import sha256_bytes
from friday.organs.engineer.command.resolve import attest_trusted_path, resolve_held, resolve_named
from friday.organs.engineer.command.spawn_helper import SpawnBroker

GRANT_SECRET = b"friday-engineer-command-kernel-tests-secret"
SOURCE_SECRET = b"friday-engineer-owner-source-tests-secret"
CONFIRM_SECRET = b"friday-engineer-owner-confirm-tests-secret"
ACTOR = "owner-1"
SOURCE_HASH = sha256_bytes(b"owner-turn-body")


def _authority(clock=None) -> CommandGrantAuthority:
    source = OwnerSourceAuthority(SOURCE_SECRET)
    confirm = OwnerConfirmationAuthority(CONFIRM_SECRET, clock=clock)
    if clock is not None:
        return CommandGrantAuthority(GRANT_SECRET, source, confirm, clock=clock)
    return CommandGrantAuthority(GRANT_SECRET, source, confirm)


def _kernel(tmp_path: Path, clock=None, *, trusted_path: TrustedPathContract | None = None) -> CommandKernel:
    return CommandKernel(tmp_path / "command-store", _authority(clock), trusted_path=trusted_path)


def _key(name: str) -> str:
    return f"idem-{name}-{time.time_ns()}"


def _argv(*argv: str, key: str, **kwargs) -> CommandRequest:
    return CommandRequest(
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        argv=argv,
        idempotency_key=key,
        **kwargs,
    )


def _shell(command: str, key: str, **kwargs) -> CommandRequest:
    return CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.OWNER_TURN,
        shell_command=command,
        idempotency_key=key,
        **kwargs,
    )


def _attest(source_auth: OwnerSourceAuthority, request: CommandRequest, **kwargs) -> OwnerSource:
    return source_auth.attest(
        actor_id=kwargs.get("actor_id", ACTOR),
        tenant_id="tenant-1",
        conversation_id="conv-1",
        channel="cli_test",
        source_row_id="row-1",
        source_hash=kwargs.get("source_hash", SOURCE_HASH),
        telegram_update_id="upd-1",
        isolation_profile=kwargs.get("isolation_profile", IsolationProfile.ISOLATED_WORKSPACE),
        idempotency_key=request.idempotency_key,
    )


def _confirm(kernel: CommandKernel, source: OwnerSource, request: CommandRequest, **kwargs):
    clock = kernel.authority.confirm_authority._clock
    expires_at = kwargs.get("expires_at", int(clock()) + 60)
    event_marker = f"{request.idempotency_key}-{time.time_ns()}"
    handle = kernel.authority.confirm_authority.ingest(
        actor_id=source.actor_id,
        tenant_id=source.tenant_id,
        conversation_id=source.conversation_id,
        channel=source.channel,
        confirmation_row_id=kwargs.get("confirmation_row_id", f"confirm-row-{event_marker}"),
        confirmation_update_id=kwargs.get("confirmation_update_id", f"confirm-upd-{event_marker}"),
        command_digest=request.digest,
        body_hash=kwargs.get("body_hash", sha256_bytes(b"confirm-body")),
        expires_at=int(expires_at),
    )
    return kernel.authority.confirm_authority.seal(handle, command_digest=request.digest)


def _submit(kernel: CommandKernel, request: CommandRequest, **kwargs) -> str:
    source_auth = kernel.authority.source_authority
    isolation = kwargs.pop("isolation_profile", IsolationProfile.ISOLATED_WORKSPACE)
    kwargs.pop("host_user_authorized", None)
    actor_id = kwargs.pop("actor_id", ACTOR)
    source = _attest(
        source_auth,
        request,
        isolation_profile=isolation,
        actor_id=actor_id,
    )
    confirmation = None
    if kwargs.pop("destructive", True):
        confirmation = _confirm(kernel, source, request)
    token = kernel.authority.issue(request, source=source, confirmation=confirmation)
    return kernel.submit(request, token, actor_id=actor_id)


def _wait(kernel: CommandKernel, job_id: str):
    return kernel.wait(job_id, actor_id=ACTOR)


def test_argv_echo_completes_without_inheriting_caller_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRIDAY_SHOULD_NOT_LEAK", "secret-value")
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/env", key=_key("env"))
    job_id = _submit(kernel, request)
    receipt = _wait(kernel, job_id)
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.exit_code == 0
    assert receipt.authorization_complete is False
    assert receipt.effect_boundary_crossed is True
    assert receipt.isolation_profile is IsolationProfile.ISOLATED_WORKSPACE
    assert receipt.to_public_payload()["isolated"] is True
    text = receipt.stdout.decode()
    assert "FRIDAY_SHOULD_NOT_LEAK" not in text
    assert "PATH=/usr/bin:/bin" in text
    assert "argv" not in receipt.to_public_payload()
    assert "shell_command" not in receipt.to_public_payload()
    assert receipt.to_public_payload()["authorization_complete"] is False
    assert receipt.receipt_mac


def test_shell_writes_admitted_output_file(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf 'hello\\n' > output/note.txt", key=_key("out"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED
    assert len(receipt.generated_files) == 1
    generated = receipt.generated_files[0]
    assert generated.relative_path == "note.txt"
    assert generated.size_bytes == 6
    sealed = kernel.store.job_dir(receipt.job_id) / "sealed" / "note.txt"
    assert sealed.read_bytes() == b"hello\n"


def test_long_job_progress_is_truthful_then_cancel(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "20", key=_key("sleep"), timeout_sec=30)
    job_id = _submit(kernel, request)
    first = kernel.progress(job_id, actor_id=ACTOR)
    assert first.status is CommandStatus.RUNNING
    assert first.percent is None
    assert first.eta_sec is None
    time.sleep(0.2)
    second = kernel.progress(job_id, actor_id=ACTOR)
    assert second.elapsed_sec >= first.elapsed_sec
    kernel.cancel(job_id, actor_id=ACTOR)
    receipt = _wait(kernel, job_id)
    assert receipt.status is CommandStatus.CANCELLED
    assert receipt.cancelled is True
    assert receipt.effect_boundary_crossed is True


def test_timeout_is_not_reported_as_success(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "20", key=_key("timeout"), timeout_sec=1)
    receipt = _wait(kernel, _submit(kernel, request))
    assert receipt.status is CommandStatus.TIMEOUT
    assert receipt.timed_out is True
    assert receipt.exit_code != 0 or receipt.signal is not None


def test_stdin_reaches_cat(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/cat", key=_key("stdin"), stdin=b"payload-bytes")
    receipt = _wait(kernel, _submit(kernel, request))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.stdout == b"payload-bytes"


def test_idempotent_submit_returns_same_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    key = _key("idem")
    request = _argv("/usr/bin/true", key=key)
    first = _submit(kernel, request)
    _wait(kernel, first)
    second = kernel.submit(request, "not-a-grant", actor_id=ACTOR)
    assert second == first


def test_idempotency_conflict_on_different_digest(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    key = _key("conflict")
    first_req = _argv("/usr/bin/true", key=key)
    _submit(kernel, first_req)
    second_req = _argv("/usr/bin/false", key=key)
    with pytest.raises(CommandError, match="idempotency_conflict"):
        _submit(kernel, second_req)


def test_restart_without_live_pid_is_unknown(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("restart"))
    job_id = _submit(kernel, request)
    _wait(kernel, job_id)
    conn = sqlite3.connect(str(kernel.store.db_path))
    conn.execute(
        "UPDATE jobs SET status='running', pid=999999, pid_starttime=1, finished_at=NULL WHERE job_id=?",
        (job_id,),
    )
    conn.commit()
    conn.close()
    store_root = kernel.store.root
    kernel.close()
    restarted = CommandKernel(store_root, _authority())
    progress = restarted.progress(job_id, actor_id=ACTOR)
    assert progress.status is CommandStatus.UNKNOWN
    receipt = restarted.wait(job_id, actor_id=ACTOR)
    assert receipt.status is CommandStatus.UNKNOWN
    assert receipt.error_code == "unknown_after_restart"
    assert receipt.effect_boundary_crossed is True


def test_restart_does_not_trust_reused_pid(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("pid-reuse"))
    job_id = _submit(kernel, request)
    _wait(kernel, job_id)
    conn = sqlite3.connect(str(kernel.store.db_path))
    conn.execute(
        "UPDATE jobs SET status='running', pid=?, pid_starttime=1, finished_at=NULL WHERE job_id=?",
        (os.getpid(), job_id),
    )
    conn.commit()
    conn.close()
    store_root = kernel.store.root
    kernel.close()
    restarted = CommandKernel(store_root, _authority())
    assert restarted.progress(job_id, actor_id=ACTOR).status is CommandStatus.UNKNOWN
    assert os.getpid() > 0


def test_missing_or_forged_grant_is_refused(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("grant"))
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.submit(request, "forged.token", actor_id=ACTOR)
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source)
    tampered = token[:-2] + ("0" if token[-2] != "0" else "1") + token[-1]
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.submit(request, tampered, actor_id=ACTOR)


def test_grant_replay_and_digest_mismatch_and_actor_mismatch(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("replay"))
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))
    _wait(kernel, kernel.submit(request, token, actor_id=ACTOR))
    same_digest = _argv("/usr/bin/true", key=_key("replay-2"))
    with pytest.raises(CommandError, match="grant_replay|grant_idempotency_mismatch"):
        kernel.submit(same_digest, token, actor_id=ACTOR)
    other = _argv("/usr/bin/false", key=_key("replay-3"))
    other_source = _attest(kernel.authority.source_authority, other)
    with pytest.raises(CommandError, match="grant_actor_mismatch"):
        kernel.submit(other, kernel.authority.issue(other, source=other_source), actor_id="other")
    mismatched = kernel.authority.issue(request, source=_attest(kernel.authority.source_authority, request))
    with pytest.raises(CommandError, match="grant_command_mismatch"):
        kernel.submit(other, mismatched, actor_id=ACTOR)


def test_expired_grant_is_refused(tmp_path: Path) -> None:
    clock = {"now": 1_000}

    def _now() -> int:
        return int(clock["now"])

    kernel = _kernel(tmp_path, clock=_now)
    request = _argv("/usr/bin/true", key=_key("exp"))
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source, ttl_sec=30)
    clock["now"] = 1_040
    with pytest.raises(CommandError, match="grant_expired"):
        kernel.submit(request, token, actor_id=ACTOR)


def test_non_owner_origins_cannot_issue(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    for origin in (
        CommandOrigin.MODEL,
        CommandOrigin.DOCUMENT,
        CommandOrigin.WEB,
        CommandOrigin.MEMORY,
        CommandOrigin.ATTACHMENT,
    ):
        request = CommandRequest(
            lane=CommandLane.SHELL if origin is not CommandOrigin.MODEL else CommandLane.ARGV,
            origin=origin,
            argv=() if origin is not CommandOrigin.MODEL else ("/usr/bin/true",),
            shell_command="true" if origin is not CommandOrigin.MODEL else None,
            idempotency_key=_key(origin.value),
        )
        source = _attest(kernel.authority.source_authority, request)
        with pytest.raises(CommandError, match="owner_origin_required"):
            kernel.authority.issue(request, source=source)


def test_forged_owner_source_is_refused(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("forged-source"))
    forged = OwnerSource(
        actor_id=ACTOR,
        tenant_id="tenant-1",
        conversation_id="conv-1",
        channel="cli_test",
        source_row_id="row-1",
        source_hash=SOURCE_HASH,
        telegram_update_id="upd-1",
        isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        idempotency_key=request.idempotency_key,
        mac="00" * 32,
    )
    with pytest.raises(CommandError, match="invalid_owner_source"):
        kernel.authority.issue(request, source=forged)


def test_boolean_cannot_satisfy_destructive_approval(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("sudo -n true", key=_key("bool-destructive"))
    source = _attest(kernel.authority.source_authority, request)
    with pytest.raises(TypeError):
        kernel.authority.issue(request, source=source, destructive_confirmed=True)  # type: ignore[call-arg]
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        _submit(kernel, request, destructive=False)


def test_symlink_executable_is_refused(tmp_path: Path) -> None:
    alias = tmp_path / "true-link"
    alias.symlink_to("/usr/bin/true")
    with pytest.raises(CommandError, match="symlink_refused"):
        resolve_named(str(alias))


def test_writable_executable_is_refused(tmp_path: Path) -> None:
    script = tmp_path / "writable.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    script.chmod(0o777)
    with pytest.raises(CommandError, match="writable_executable"):
        resolve_named(str(script))


def test_env_shebang_is_refused(tmp_path: Path) -> None:
    script = tmp_path / "env.sh"
    script.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(CommandError, match="env_shebang_refused"):
        resolve_named(str(script))


def test_path_escape_and_docker_socket_are_refused(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    escape = _argv("../usr/bin/true", key=_key("escape"))
    with pytest.raises(CommandError, match="relative_name_invalid|path_escape"):
        _submit(kernel, escape)
    sock = _argv("/usr/bin/true", "/var/run/docker.sock", key=_key("sock"))
    with pytest.raises(CommandError, match="forbidden_path"):
        _submit(kernel, sock)


def test_sudo_and_destructive_shell_need_confirmation(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    sudo_path = Path("/usr/bin/sudo")
    if sudo_path.exists():
        request = _argv("/usr/bin/sudo", "-n", "true", key=_key("sudo"))
        with pytest.raises(CommandError, match="setid_refused|destructive_confirmation_required|symlink_refused"):
            _submit(kernel, request, destructive=False)
    shell = _shell("sudo -n true", key=_key("sudo-shell"))
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        _submit(kernel, shell, destructive=False)
    confirmed = _shell("printf x > output/ok.txt", key=_key("ok-shell"), timeout_sec=10)
    receipt = _wait(kernel, _submit(kernel, confirmed, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED


def test_owner_shell_pipeline_and_fork_cancel(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    pipeline = _shell("printf 'a\\n' | cat > output/pipe.txt", key=_key("pipe"))
    receipt = _wait(kernel, _submit(kernel, pipeline, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.generated_files[0].relative_path == "pipe.txt"
    assert receipt.to_public_payload()["shell_subcommands_attested"] is False
    request = _shell("sleep 20 & sleep 20; wait", key=_key("forks"), timeout_sec=30)
    job_id = _submit(kernel, request, destructive=True)
    kernel.cancel(job_id, actor_id=ACTOR)
    cancelled = _wait(kernel, job_id)
    assert cancelled.status is CommandStatus.CANCELLED
    assert cancelled.effect_boundary_crossed is True


def test_nonce_cannot_be_consumed_twice(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    with kernel.store.transaction():
        kernel.store.consume_nonce("abcd" * 8, exp=2**31, now=1)
        with pytest.raises(CommandError, match="grant_replay"):
            kernel.store.consume_nonce("abcd" * 8, exp=2**31, now=1)


def test_grant_replay_survives_kernel_restart(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("persist-replay"))
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))
    _wait(kernel, kernel.submit(request, token, actor_id=ACTOR))
    store_root = kernel.store.root
    kernel.close()
    restarted = CommandKernel(store_root, _authority())
    same_digest = _argv("/usr/bin/true", key=_key("persist-replay-2"))
    with pytest.raises(CommandError, match="grant_replay|grant_idempotency_mismatch"):
        restarted.submit(same_digest, token, actor_id=ACTOR)


def test_late_revoke_refuses_spawn(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("revoke"))
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source)
    kernel.authority.revoke(token)
    with pytest.raises(CommandError, match="grant_revoked"):
        kernel.submit(request, token, actor_id=ACTOR)


def test_output_symlink_is_not_admitted(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("ln -s /etc/passwd output/stolen", key=_key("symlink-out"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code == "output_symlink_refused"
    assert receipt.generated_files == ()


def test_stdout_truncation_is_honest(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv(
        "/usr/bin/python3",
        "-c",
        "import sys; sys.stdout.buffer.write(b'x' * 8192)",
        key=_key("trunc"),
        max_stdout_bytes=1024,
        timeout_sec=10,
    )
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.truncated_stdout is True
    assert len(receipt.stdout) == 1024


def test_user_script_with_direct_shebang_runs(tmp_path: Path) -> None:
    script = tmp_path / "ok.sh"
    script.write_text("#!/usr/bin/bash\nprintf script-ok\n", encoding="utf-8")
    script.chmod(0o755)
    kernel = _kernel(tmp_path)
    request = _argv(str(script), key=_key("script"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.stdout == b"script-ok"


def test_kernel_source_does_not_import_host_control_or_nmap() -> None:
    root = Path(__file__).resolve().parents[1] / "friday" / "organs" / "engineer" / "command"
    for path in sorted(root.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            assert "host_control" not in stripped
            assert "HostJobStore" not in stripped
            assert "nmap" not in stripped
            assert "execution_authority" not in stripped


def test_regular_file_mode_bits_are_not_treated_as_attestation_alone(tmp_path: Path) -> None:
    script = tmp_path / "owner-writable-but-group-clean.sh"
    script.write_text("#!/usr/bin/bash\nprintf ok\n", encoding="utf-8")
    os.chmod(script, 0o755)
    resolved = resolve_named(str(script))
    assert stat.S_IMODE(resolved.mode) == 0o755
    assert resolved.sha256
    os.chmod(script, 0o775)
    with pytest.raises(CommandError, match="writable_executable"):
        resolve_named(str(script))


def test_isolated_workspace_denies_host_files_and_network(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    secret = tmp_path / "host-secret.txt"
    secret.write_text("top-secret\n", encoding="utf-8")
    leak = _argv(
        "/usr/bin/python3",
        "-c",
        f"import pathlib,sys; p=pathlib.Path({str(secret)!r}); sys.exit(2 if p.exists() else 0)",
        key=_key("host-file"),
    )
    leak_receipt = _wait(kernel, _submit(kernel, leak, destructive=True))
    assert leak_receipt.status is CommandStatus.COMPLETED
    assert leak_receipt.exit_code == 0
    net = _argv(
        "/usr/bin/python3",
        "-c",
        "import socket,sys\n"
        "try:\n"
        " socket.create_connection(('1.1.1.1', 80), 1)\n"
        " sys.exit(42)\n"
        "except Exception:\n"
        " sys.exit(0)\n",
        key=_key("net"),
        timeout_sec=10,
    )
    net_receipt = _wait(kernel, _submit(kernel, net, destructive=True))
    assert net_receipt.status is CommandStatus.COMPLETED
    assert net_receipt.exit_code == 0


def test_host_user_profile_requires_broker(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("host-user"))
    with pytest.raises(CommandError, match="host_user_requires_broker"):
        _attest(
            kernel.authority.source_authority,
            request,
            isolation_profile=IsolationProfile.HOST_USER,
        )
    with pytest.raises(TypeError):
        kernel.authority.source_authority.attest(  # type: ignore[call-arg]
            actor_id=ACTOR,
            tenant_id="tenant-1",
            conversation_id="conv-1",
            channel="cli_test",
            source_row_id="row-1",
            source_hash=SOURCE_HASH,
            telegram_update_id="upd-1",
            isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
            idempotency_key=request.idempotency_key,
            host_user_authorized=True,
        )
    assert not hasattr(kernel.authority.source_authority, "approve_destructive")


def test_leader_exit_does_not_leave_descendants(tmp_path: Path) -> None:
    marker = f"9177{time.time_ns() % 100000}"
    kernel = _kernel(tmp_path)
    request = _shell(f"/usr/bin/sleep {marker} & exit 0", key=_key("orphans"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED
    leftover = [
        line
        for line in Path("/proc").iterdir()
        if line.name.isdigit()
        and (line / "cmdline").exists()
        and marker.encode() in (line / "cmdline").read_bytes()
    ]
    assert leftover == []


def test_held_fd_survives_executable_and_interpreter_swap(tmp_path: Path) -> None:
    from friday.organs.engineer.command.resolve import confirm_held, resolve_bwrap
    from friday.organs.engineer.command.runner import SpawnedCommand
    from friday.organs.engineer.command.workspace import JobWorkspace

    interp = tmp_path / "interp"
    shutil.copy("/usr/bin/bash", interp)
    interp.chmod(0o755)
    script = tmp_path / "payload.sh"
    script.write_text(f"#!{interp}\nprintf FROM-HELD\n", encoding="utf-8")
    script.chmod(0o755)
    held = resolve_held(str(script))
    evil = tmp_path / "evil"
    evil.write_text("#!/usr/bin/bash\nprintf HACKED\n", encoding="utf-8")
    evil.chmod(0o755)
    os.replace(evil, interp)
    swapped = tmp_path / "swapped.sh"
    swapped.write_text("#!/usr/bin/bash\nprintf SWAPPED-SCRIPT\n", encoding="utf-8")
    swapped.chmod(0o755)
    os.replace(swapped, script)
    confirm_held(held)
    job_dir = tmp_path / "swap-job"
    job_dir.mkdir()
    os.chmod(job_dir, 0o700)
    workspace = JobWorkspace(job_dir)
    workspace.materialize()
    spawned = SpawnedCommand(
        workspace=workspace,
        timeout_sec=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
    )
    bwrap = resolve_bwrap()
    broker = SpawnBroker()
    scope = SystemdCgroupBoundary().allocate(secrets.token_hex(16), ResourceLimits.default(), timeout_sec=10)
    try:
        spawned.spawn(
            held,
            stdin=b"",
            env=workspace.env(path_value="/usr/bin:/bin", isolated=True),
            path_roots=attest_trusted_path(TrustedPathContract.default()),
            bwrap=bwrap,
            scope=scope,
            broker=broker,
        )
        spawned.wait()
    finally:
        held.close()
        bwrap.close()
        spawned.close_pidfd()
        scope.kill()
        broker.close()
    assert spawned.exit_code == 0
    assert spawned.stdout == b"FROM-HELD"
    assert b"HACKED" not in spawned.stdout
    assert b"SWAPPED" not in spawned.stdout


def test_concurrent_same_key_submit_is_single_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    key = _key("concurrent")
    request = _argv("/usr/bin/sleep", "2", key=key, timeout_sec=10)
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))
    first = kernel.submit(request, token, actor_id=ACTOR)
    second = kernel.submit(request, token, actor_id=ACTOR)
    assert first == second
    kernel.cancel(first, actor_id=ACTOR)
    receipt = _wait(kernel, first)
    assert receipt.status in {CommandStatus.CANCELLED, CommandStatus.COMPLETED}


def test_same_uid_symlink_tmp_does_not_capture_ledger(tmp_path: Path) -> None:
    store_root = tmp_path / "command-store"
    store_root.mkdir()
    os.chmod(store_root, 0o700)
    bait = tmp_path / "stolen"
    bait.mkdir()
    (store_root / "kernel.lock").symlink_to(bait / "lock")
    with pytest.raises(CommandError):
        CommandKernel(store_root, _authority())


def test_progress_cancel_wait_require_actor(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "10", key=_key("actor"), timeout_sec=20)
    job_id = _submit(kernel, request)
    with pytest.raises(CommandError, match="actor_mismatch"):
        kernel.progress(job_id, actor_id="other")
    with pytest.raises(CommandError, match="actor_mismatch"):
        kernel.cancel(job_id, actor_id="other")
    with pytest.raises(CommandError, match="actor_mismatch"):
        kernel.wait(job_id, actor_id="other")
    kernel.cancel(job_id, actor_id=ACTOR)
    _wait(kernel, job_id)


def test_trusted_path_is_used_for_resolve_and_runtime(tmp_path: Path) -> None:
    extra = tmp_path / "svcbin"
    extra.mkdir()
    os.chmod(extra, 0o755)
    tool = extra / "svc-echo"
    tool.write_text("#!/usr/bin/bash\nprintf SVC:$PATH\n", encoding="utf-8")
    tool.chmod(0o755)
    contract = TrustedPathContract(directories=("/usr/bin", "/bin", str(extra)))
    kernel = _kernel(tmp_path, trusted_path=contract)
    request = _argv("svc-echo", key=_key("svcpath"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.COMPLETED
    text = receipt.stdout.decode()
    assert str(extra) in text
    assert text.startswith("SVC:")
    ambient = _argv(
        "/usr/bin/python3",
        "-c",
        "import os; print(os.environ.get('UNTRUSTED_PATH_VAR','missing'))",
        key=_key("no-ambient"),
    )
    env_receipt = _wait(kernel, _submit(kernel, ambient, destructive=True))
    assert b"missing" in env_receipt.stdout


def test_durable_state_omits_secret_bearing_argv(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf 'secret-in-shell'\n", key=_key("no-argv"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    job = kernel.store.read_job(receipt.job_id)
    blob = str(job)
    assert "secret-in-shell" not in blob
    assert "printf" not in blob
    assert job["command_digest"] == request.digest


def test_minting_api_rejects_boolean_and_unverified_hash(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("sudo -n true", key=_key("hash-claim"))
    source = _attest(kernel.authority.source_authority, request)
    with pytest.raises(TypeError):
        kernel.authority.confirm_authority.seal(  # type: ignore[call-arg]
            actor_id=source.actor_id,
            tenant_id=source.tenant_id,
            conversation_id=source.conversation_id,
            channel=source.channel,
            confirmation_row_id="confirm-row-1",
            confirmation_update_id="confirm-upd-1",
            command_digest=request.digest,
            expires_at=int(time.time()) + 60,
            confirmation_hash="00" * 32,
        )
    forged = _confirm(kernel, source, request)
    from friday.organs.engineer.command.contracts import OwnerConfirmation

    bad = OwnerConfirmation(
        actor_id=forged.actor_id,
        tenant_id=forged.tenant_id,
        conversation_id=forged.conversation_id,
        channel=forged.channel,
        confirmation_row_id=forged.confirmation_row_id,
        confirmation_update_id=forged.confirmation_update_id,
        command_digest=forged.command_digest,
        expires_at=forged.expires_at,
        nonce=forged.nonce,
        mac="00" * 32,
    )
    with pytest.raises(CommandError, match="invalid_destructive_approval"):
        kernel.authority.issue(request, source=source, confirmation=bad)
    same_row = _confirm(
        kernel,
        source,
        request,
        confirmation_row_id=source.source_row_id,
        confirmation_update_id=source.telegram_update_id,
    )
    with pytest.raises(CommandError, match="confirmation_not_distinct"):
        kernel.authority.issue(request, source=source, confirmation=same_row)


def test_same_inode_rewrite_does_not_change_sealed_snapshot(tmp_path: Path) -> None:
    def _rewrite(path: Path, payload: bytes) -> None:
        fd = os.open(str(path), os.O_WRONLY)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    script = tmp_path / "payload.sh"
    first = b"#!/usr/bin/bash\nprintf FIRST-BYTES\n"
    second = b"#!/usr/bin/bash\nprintf SECOND-NOW\n"
    pad = 64 + max(len(first), len(second))
    first = first + b"#" * (pad - len(first))
    second = second + b"#" * (pad - len(second))
    assert len(first) == len(second)
    script.write_bytes(first)
    script.chmod(0o755)
    held = resolve_held(str(script))
    _rewrite(script, second)
    from friday.organs.engineer.command.resolve import confirm_held, resolve_bwrap
    from friday.organs.engineer.command.runner import SpawnedCommand
    from friday.organs.engineer.command.workspace import JobWorkspace

    confirm_held(held)
    job_dir = tmp_path / "rewrite-job"
    job_dir.mkdir()
    os.chmod(job_dir, 0o700)
    workspace = JobWorkspace(job_dir)
    workspace.materialize()
    spawned = SpawnedCommand(
        workspace=workspace,
        timeout_sec=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
    )
    bwrap = resolve_bwrap()
    broker = SpawnBroker()
    scope = SystemdCgroupBoundary().allocate(secrets.token_hex(16), ResourceLimits.default(), timeout_sec=10)
    try:
        spawned.spawn(
            held,
            stdin=b"",
            env=workspace.env(path_value="/usr/bin:/bin", isolated=True),
            path_roots=attest_trusted_path(TrustedPathContract.default()),
            bwrap=bwrap,
            scope=scope,
            broker=broker,
        )
        spawned.wait()
    finally:
        held.close()
        bwrap.close()
        spawned.close_pidfd()
        scope.kill()
    assert spawned.exit_code == 0
    assert spawned.stdout == b"FIRST-BYTES"
    assert b"SECOND" not in spawned.stdout

    elf = tmp_path / "elf-true"
    shutil.copy("/usr/bin/true", elf)
    elf.chmod(0o755)
    held_elf = resolve_held(str(elf))
    original = elf.read_bytes()
    mutated = b"\x00" + original[1:]
    assert len(mutated) == len(original)
    _rewrite(elf, mutated)
    confirm_held(held_elf)
    job_dir2 = tmp_path / "rewrite-elf"
    job_dir2.mkdir()
    os.chmod(job_dir2, 0o700)
    workspace2 = JobWorkspace(job_dir2)
    workspace2.materialize()
    spawned2 = SpawnedCommand(
        workspace=workspace2,
        timeout_sec=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
    )
    bwrap2 = resolve_bwrap()
    scope2 = SystemdCgroupBoundary().allocate(secrets.token_hex(16), ResourceLimits.default(), timeout_sec=10)
    try:
        spawned2.spawn(
            held_elf,
            stdin=b"",
            env=workspace2.env(path_value="/usr/bin:/bin", isolated=True),
            path_roots=attest_trusted_path(TrustedPathContract.default()),
            bwrap=bwrap2,
            scope=scope2,
            broker=broker,
        )
        spawned2.wait()
    finally:
        held_elf.close()
        bwrap2.close()
        spawned2.close_pidfd()
        scope2.kill()
    assert spawned2.exit_code == 0

    interp = tmp_path / "interp-bash"
    shutil.copy("/usr/bin/bash", interp)
    interp.chmod(0o755)
    body = tmp_path / "interp-payload.sh"
    shebang = f"#!{interp}\nprintf FROM-INTERP\n".encode()
    body.write_bytes(shebang)
    body.chmod(0o755)
    held_script = resolve_held(str(body))
    evil = shutil.copy("/usr/bin/false", tmp_path / "evil-false")
    Path(evil).chmod(0o755)
    # same-inode rewrite of interpreter: overwrite leading bytes, keep size
    interp_bytes = interp.read_bytes()
    _rewrite(interp, b"\x00" + interp_bytes[1:])
    swapped = tmp_path / "swapped-payload.sh"
    swapped.write_bytes(b"#!/usr/bin/bash\nprintf SWAPPED\n" + b"#" * 32)
    os.replace(swapped, body)
    confirm_held(held_script)
    job_dir3 = tmp_path / "rewrite-interp"
    job_dir3.mkdir()
    os.chmod(job_dir3, 0o700)
    workspace3 = JobWorkspace(job_dir3)
    workspace3.materialize()
    spawned3 = SpawnedCommand(
        workspace=workspace3,
        timeout_sec=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
    )
    bwrap3 = resolve_bwrap()
    scope3 = SystemdCgroupBoundary().allocate(secrets.token_hex(16), ResourceLimits.default(), timeout_sec=10)
    try:
        spawned3.spawn(
            held_script,
            stdin=b"",
            env=workspace3.env(path_value="/usr/bin:/bin", isolated=True),
            path_roots=attest_trusted_path(TrustedPathContract.default()),
            bwrap=bwrap3,
            scope=scope3,
            broker=broker,
        )
        spawned3.wait()
    finally:
        held_script.close()
        bwrap3.close()
        spawned3.close_pidfd()
        scope3.kill()
        broker.close()
    assert spawned3.exit_code == 0
    assert spawned3.stdout == b"FROM-INTERP"


def test_missing_controller_fails_closed(tmp_path: Path) -> None:
    kernel = CommandKernel(
        tmp_path / "command-store",
        _authority(),
        boundary=MissingControllerBoundary(),
    )
    request = _argv("/usr/bin/true", key=_key("missing-ctl"))
    with pytest.raises(CommandError, match="resource_boundary_unproven"):
        _submit(kernel, request)


def test_fork_bomb_is_contained(tmp_path: Path) -> None:
    kernel = CommandKernel(
        tmp_path / "command-store",
        _authority(),
        limits=ResourceLimits(tasks_max=8, memory_max=64 * 1024 * 1024, cpu_quota_percent=50),
    )
    request = _argv(
        "/usr/bin/python3",
        "-c",
        "import os\nwhile True:\n os.fork()\n",
        key=_key("fork-bomb"),
        timeout_sec=8,
    )
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status in {CommandStatus.FAILED, CommandStatus.TIMEOUT, CommandStatus.UNKNOWN}
    assert receipt.status is not CommandStatus.COMPLETED


def test_tmpfs_and_output_quota_kill(tmp_path: Path) -> None:
    kernel = CommandKernel(
        tmp_path / "command-store",
        _authority(),
        limits=ResourceLimits(tmpfs_tmp=65536, tmpfs_workspace=65536, tmpfs_job_tmp=32768),
    )
    tmpfs = _argv(
        "/usr/bin/python3",
        "-c",
        "import errno,sys\n"
        "try:\n"
        " f=open('/tmp/blob','wb')\n"
        " while True:\n"
        "  f.write(b'x'*4096)\n"
        "except OSError as exc:\n"
        " sys.exit(0 if exc.errno==errno.ENOSPC else 2)\n",
        key=_key("tmpfs-mem"),
        timeout_sec=10,
    )
    tmpfs_receipt = _wait(kernel, _submit(kernel, tmpfs, destructive=True))
    assert tmpfs_receipt.status is CommandStatus.COMPLETED
    many = _argv(
        "/usr/bin/python3",
        "-c",
        "from pathlib import Path\n"
        "p=Path('output')\n"
        "for i in range(200):\n"
        " (p/f'f{i}').write_text('n')\n",
        key=_key("many-files"),
        timeout_sec=10,
    )
    many_receipt = _wait(kernel, _submit(kernel, many, destructive=True))
    assert many_receipt.status is CommandStatus.FAILED
    assert many_receipt.error_code in {"output_quota_exceeded", "output_tree_overflow"}
    huge = _argv(
        "/usr/bin/python3",
        "-c",
        "open('output/big','wb').write(b'x'*(40*1024*1024))",
        key=_key("agg-bytes"),
        timeout_sec=15,
    )
    huge_receipt = _wait(kernel, _submit(kernel, huge, destructive=True))
    assert huge_receipt.status is CommandStatus.FAILED
    assert huge_receipt.error_code in {
        "output_quota_exceeded",
        "output_tree_overflow",
        "output_file_too_large",
        "nonzero_exit",
    }


def test_post_spawn_commit_failure_kills_process(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    kernel.store.fail_next_commit = 3
    marker = f"31.{time.time_ns() % 10**9}"
    request = _argv("/usr/bin/sleep", marker, key=_key("orphan-window"), timeout_sec=20)
    with pytest.raises(CommandError, match="unknown_after_spawn|durable_write_failed"):
        _submit(kernel, request)
    leftover = []
    for line in Path("/proc").iterdir():
        if not line.name.isdigit():
            continue
        try:
            if marker.encode() in (line / "cmdline").read_bytes():
                leftover.append(line)
        except (OSError, ProcessLookupError):
            continue
    # The abort path must not leave the 30s sleep from this test.
    assert leftover == []


def test_restart_receipt_rejects_mutated_evidence(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/cat", key=_key("evidence"), stdin=b"stable-bytes")
    receipt = _wait(kernel, _submit(kernel, request))
    assert receipt.status is CommandStatus.COMPLETED
    job_dir = kernel.store.job_dir(receipt.job_id)
    stdout = job_dir / "evidence" / "stdout.bin"
    store_root = kernel.store.root
    kernel.close()

    os.remove(stdout)
    stdout.symlink_to("/etc/passwd")
    restarted = CommandKernel(store_root, _authority())
    bad = restarted.wait(receipt.job_id, actor_id=ACTOR)
    assert bad.status is CommandStatus.UNKNOWN
    assert bad.error_code == "corrupt_evidence"
    restarted.close()

    os.remove(stdout)
    stdout.write_bytes(b"stable-bytes")
    stdout.write_bytes(b"stable-bytes" + b"!")
    restarted2 = CommandKernel(store_root, _authority())
    replaced = restarted2.wait(receipt.job_id, actor_id=ACTOR)
    assert replaced.status is CommandStatus.UNKNOWN
    assert replaced.error_code == "corrupt_evidence"
    restarted2.close()

    stdout.write_bytes(b"stable-bytes")
    with stdout.open("ab") as handle:
        handle.write(b"appended")
    restarted3 = CommandKernel(store_root, _authority())
    appended = restarted3.wait(receipt.job_id, actor_id=ACTOR)
    assert appended.status is CommandStatus.UNKNOWN
    restarted3.close()

    stdout.write_bytes(b"x" * (2 * 1024 * 1024 + 10))
    restarted4 = CommandKernel(store_root, _authority())
    oversized = restarted4.wait(receipt.job_id, actor_id=ACTOR)
    assert oversized.status is CommandStatus.UNKNOWN


def test_confirmation_row_or_update_reuse_is_not_distinct(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("true", key=_key("distinct"))
    source = _attest(kernel.authority.source_authority, request)
    same_row = _confirm(
        kernel,
        source,
        request,
        confirmation_row_id=source.source_row_id,
        confirmation_update_id="other-upd",
    )
    with pytest.raises(CommandError, match="confirmation_not_distinct"):
        kernel.authority.issue(request, source=source, confirmation=same_row)
    same_update = _confirm(
        kernel,
        source,
        request,
        confirmation_row_id="other-row",
        confirmation_update_id=source.telegram_update_id,
    )
    with pytest.raises(CommandError, match="confirmation_not_distinct"):
        kernel.authority.issue(request, source=source, confirmation=same_update)


def test_grant_expires_when_confirmation_expires(tmp_path: Path) -> None:
    clock = {"now": 1_000}

    def _now() -> int:
        return int(clock["now"])

    kernel = _kernel(tmp_path, clock=_now)
    request = _shell("true", key=_key("confirm-exp"))
    source = _attest(kernel.authority.source_authority, request)
    confirmation = _confirm(kernel, source, request, expires_at=1_001)
    token = kernel.authority.issue(request, source=source, confirmation=confirmation, ttl_sec=90)
    clock["now"] = 1_002
    with pytest.raises(CommandError, match="confirmation_expired|grant_expired"):
        kernel.submit(request, token, actor_id=ACTOR)


def test_shell_bypass_strings_require_confirmation(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    for label, command in (
        ("quote-sudo", "sud''o -n true"),
        ("systemctl", "/usr/bin/systemctl --user stop important.service"),
        ("mount", "/usr/bin/mount -t tmpfs tmpfs /job/output"),
        ("chmod", "chmod  777 output"),
        ("plain", "printf x"),
    ):
        request = _shell(command, key=_key(label))
        with pytest.raises(CommandError, match="destructive_confirmation_required"):
            _submit(kernel, request, destructive=False)


def test_usage_walk_fail_closed_on_depth_and_unreadable(tmp_path: Path) -> None:
    from friday.organs.engineer.command.runner import _output_usage

    deep = tmp_path / "tree"
    cursor = deep
    for index in range(9):
        cursor = cursor / f"l{index}"
        cursor.mkdir(parents=True)
    (cursor / "big").write_bytes(b"x" * 33_554_433)
    with pytest.raises(CommandError, match="output_depth_overflow|output_quota_exceeded"):
        _output_usage(deep)
    linked = tmp_path / "with-link"
    linked.mkdir()
    (linked / "ok").write_text("n", encoding="utf-8")
    (linked / "sneak").symlink_to("/etc/passwd")
    with pytest.raises(CommandError, match="output_unreadable|output_symlink|output_quota"):
        _output_usage(linked)


def test_deep_output_tree_is_killed(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv(
        "/usr/bin/python3",
        "-c",
        "from pathlib import Path\n"
        "p=Path('output')\n"
        "for i in range(9):\n"
        " p=p/f'd{i}'\n"
        " p.mkdir(parents=True, exist_ok=True)\n"
        " (p/'blob').write_bytes(b'x'*65536)\n",
        key=_key("deep-out"),
        timeout_sec=15,
    )
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code in {
        "output_quota_exceeded",
        "output_depth_overflow",
        "output_tree_overflow",
        "nonzero_exit",
    }


def test_runtime_max_kills_moved_payload_tree() -> None:
    import subprocess

    limits = ResourceLimits(runtime_grace_sec=5)
    job_id = secrets.token_hex(16)
    boundary = SystemdCgroupBoundary()
    scope = boundary.allocate(job_id, limits, timeout_sec=1)
    shown = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", scope.unit, "-p", "KillMode", "-p", "Delegate"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert "KillMode=control-group" in shown.stdout
    assert "Delegate=yes" in shown.stdout
    payload = subprocess.Popen(["/usr/bin/sleep", "30"], start_new_session=True)
    try:
        boundary.move_pid(scope, payload.pid)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and payload.poll() is None:
            time.sleep(0.2)
        assert payload.poll() is not None
    finally:
        if payload.poll() is None:
            payload.kill()
            payload.wait(timeout=2)
        scope.kill()


def test_submit_from_other_thread_completes(tmp_path: Path) -> None:
    import threading

    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("other-thread"))
    source = _attest(kernel.authority.source_authority, request)
    token = kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            result["job"] = kernel.submit(request, token, actor_id=ACTOR)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    worker = threading.Thread(target=_run, name="submit-from-other")
    worker.start()
    worker.join(timeout=20)
    assert worker.is_alive() is False
    assert "exc" not in result
    receipt = _wait(kernel, str(result["job"]))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.exit_code == 0


def test_worker_start_failure_aborts_and_records_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    kernel = _kernel(tmp_path)
    original = threading.Thread.start

    def _boom(self: threading.Thread) -> None:
        if str(self.name).startswith("engineer-command-"):
            raise RuntimeError("cannot start new thread")
        original(self)

    monkeypatch.setattr(threading.Thread, "start", _boom)
    request = _argv("/usr/bin/true", key=_key("thread-fail"))
    with pytest.raises(CommandError, match="unknown_after_spawn"):
        _submit(kernel, request)
    rows = kernel.store._conn.execute("SELECT status, error_code FROM jobs").fetchall()
    assert rows
    assert str(rows[0]["status"]) == CommandStatus.UNKNOWN.value
    assert str(rows[0]["error_code"]) == "unknown_after_spawn"
    assert kernel._live == {}


def test_untrusted_path_root_is_refused(tmp_path: Path) -> None:
    extra = tmp_path / "world-writable"
    extra.mkdir()
    os.chmod(extra, 0o777)
    with pytest.raises(CommandError, match="untrusted_path_root"):
        CommandKernel(
            tmp_path / "command-store",
            _authority(),
            trusted_path=TrustedPathContract(directories=("/usr/bin", "/bin", str(extra))),
        )
