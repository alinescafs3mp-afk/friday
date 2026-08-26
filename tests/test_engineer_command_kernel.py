"""Isolated universal Engineer command kernel — PLAN-002."""

from __future__ import annotations

import json
import os
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
)
from friday.organs.engineer.command.resolve import resolve_named

SECRET = b"friday-engineer-command-kernel-tests-secret"
ACTOR = "owner-1"
TURN = "turn-1"


def _authority(clock=None) -> CommandGrantAuthority:
    return CommandGrantAuthority(SECRET, clock=clock) if clock is not None else CommandGrantAuthority(SECRET)


def _kernel(tmp_path: Path, clock=None) -> CommandKernel:
    return CommandKernel(tmp_path / "command-store", _authority(clock))


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


def _submit(kernel: CommandKernel, request: CommandRequest, **issue_kwargs) -> str:
    token = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN, **issue_kwargs)
    return kernel.submit(request, token, actor_id=ACTOR)


def test_argv_echo_completes_without_inheriting_caller_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRIDAY_SHOULD_NOT_LEAK", "secret-value")
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/env", key=_key("env"))
    job_id = _submit(kernel, request)
    receipt = kernel.wait(job_id)
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.exit_code == 0
    assert receipt.authorization_complete is False
    assert receipt.effect_boundary_crossed is True
    text = receipt.stdout.decode()
    assert "FRIDAY_SHOULD_NOT_LEAK" not in text
    assert "PATH=/usr/bin:/bin" in text
    assert receipt.to_public_payload()["authorization_complete"] is False


def test_shell_writes_admitted_output_file(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf 'hello\\n' > output/note.txt", key=_key("out"))
    receipt = kernel.wait(_submit(kernel, request))
    assert receipt.status is CommandStatus.COMPLETED
    assert len(receipt.generated_files) == 1
    generated = receipt.generated_files[0]
    assert generated.relative_path == "note.txt"
    assert generated.size_bytes == 6


def test_long_job_progress_is_truthful_then_cancel(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "20", key=_key("sleep"), timeout_sec=30)
    job_id = _submit(kernel, request)
    first = kernel.progress(job_id)
    assert first.status is CommandStatus.RUNNING
    assert first.percent is None
    assert first.eta_sec is None
    time.sleep(0.2)
    second = kernel.progress(job_id)
    assert second.elapsed_sec >= first.elapsed_sec
    kernel.cancel(job_id)
    receipt = kernel.wait(job_id)
    assert receipt.status is CommandStatus.CANCELLED
    assert receipt.cancelled is True
    assert receipt.effect_boundary_crossed is True


def test_timeout_is_not_reported_as_success(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "20", key=_key("timeout"), timeout_sec=1)
    receipt = kernel.wait(_submit(kernel, request))
    assert receipt.status is CommandStatus.TIMEOUT
    assert receipt.timed_out is True
    assert receipt.exit_code != 0 or receipt.signal is not None


def test_stdin_reaches_cat(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/cat", key=_key("stdin"), stdin=b"payload-bytes")
    receipt = kernel.wait(_submit(kernel, request))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.stdout == b"payload-bytes"


def test_idempotent_submit_returns_same_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    key = _key("idem")
    request = _argv("/usr/bin/true", key=key)
    first = _submit(kernel, request)
    kernel.wait(first)
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
    kernel.wait(job_id)
    state_path = kernel.store.job_dir(job_id) / "state.json"
    payload = json.loads(state_path.read_text(encoding="ascii"))
    payload["status"] = "running"
    payload["pid"] = 999999
    payload.pop("finished_at", None)
    state_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="ascii")
    restarted = CommandKernel(kernel.store.root, _authority())
    progress = restarted.progress(job_id)
    assert progress.status is CommandStatus.UNKNOWN
    receipt = restarted.wait(job_id)
    assert receipt.status is CommandStatus.UNKNOWN
    assert receipt.error_code == "unknown_after_restart"
    assert receipt.effect_boundary_crossed is True


def test_missing_or_forged_grant_is_refused(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("grant"))
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.submit(request, "forged.token", actor_id=ACTOR)
    token = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN)
    tampered = token[:-2] + ("0" if token[-2] != "0" else "1") + token[-1]
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.submit(request, tampered, actor_id=ACTOR)


def test_grant_replay_and_digest_mismatch_and_actor_mismatch(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("replay"))
    token = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN)
    kernel.wait(kernel.submit(request, token, actor_id=ACTOR))
    same_digest = _argv("/usr/bin/true", key=_key("replay-2"))
    with pytest.raises(CommandError, match="grant_replay"):
        kernel.submit(same_digest, token, actor_id=ACTOR)
    other = _argv("/usr/bin/false", key=_key("replay-3"))
    with pytest.raises(CommandError, match="grant_actor_mismatch"):
        kernel.submit(other, kernel.authority.issue(other, actor_id=ACTOR, turn_id=TURN), actor_id="other")
    mismatched = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN)
    with pytest.raises(CommandError, match="grant_command_mismatch"):
        kernel.submit(other, mismatched, actor_id=ACTOR)


def test_expired_grant_is_refused(tmp_path: Path) -> None:
    clock = {"now": 1_000}

    def _now() -> int:
        return int(clock["now"])

    kernel = _kernel(tmp_path, clock=_now)
    request = _argv("/usr/bin/true", key=_key("exp"))
    token = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN, ttl_sec=30)
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
        with pytest.raises(CommandError, match="owner_origin_required"):
            kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN)


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
            _submit(kernel, request)
    shell = _shell("sudo -n true", key=_key("sudo-shell"))
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        _submit(kernel, shell)
    confirmed = _shell("printf x > output/ok.txt", key=_key("ok-shell"), timeout_sec=10)
    receipt = kernel.wait(_submit(kernel, confirmed, destructive_confirmed=True))
    assert receipt.status is CommandStatus.COMPLETED


def test_owner_shell_pipeline_and_fork_cancel(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    pipeline = _shell("printf 'a\\n' | cat > output/pipe.txt", key=_key("pipe"))
    receipt = kernel.wait(_submit(kernel, pipeline))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.generated_files[0].relative_path == "pipe.txt"
    request = _shell("sleep 20 & sleep 20; wait", key=_key("forks"), timeout_sec=30)
    job_id = _submit(kernel, request)
    kernel.cancel(job_id)
    cancelled = kernel.wait(job_id)
    assert cancelled.status is CommandStatus.CANCELLED
    assert cancelled.effect_boundary_crossed is True


def test_late_revoke_refuses_spawn(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("revoke"))
    token = kernel.authority.issue(request, actor_id=ACTOR, turn_id=TURN)
    kernel.authority.revoke(token)
    with pytest.raises(CommandError, match="grant_revoked"):
        kernel.submit(request, token, actor_id=ACTOR)


def test_output_symlink_is_not_admitted(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("ln -s /etc/passwd output/stolen", key=_key("symlink-out"))
    receipt = kernel.wait(_submit(kernel, request))
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
    receipt = kernel.wait(_submit(kernel, request))
    assert receipt.truncated_stdout is True
    assert len(receipt.stdout) == 1024


def test_user_script_with_direct_shebang_runs(tmp_path: Path) -> None:
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/bash\nprintf script-ok\n", encoding="utf-8")
    script.chmod(0o755)
    kernel = _kernel(tmp_path)
    request = _argv(str(script), key=_key("script"))
    receipt = kernel.wait(_submit(kernel, request))
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
    script.write_text("#!/bin/bash\nprintf ok\n", encoding="utf-8")
    os.chmod(script, 0o755)
    resolved = resolve_named(str(script))
    assert stat.S_IMODE(resolved.mode) == 0o755
    assert resolved.sha256
    os.chmod(script, 0o775)
    with pytest.raises(CommandError, match="writable_executable"):
        resolve_named(str(script))
