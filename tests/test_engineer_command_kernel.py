"""Isolated universal Engineer command kernel — PLAN-002 / REVIEW-004."""

from __future__ import annotations

import json
import os
import pwd
import secrets
import shlex
import shutil
import sqlite3
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from friday.engineer_source_binding import (
    canonical_engineer_source_binding_sha256,
    legacy_engineer_source_binding_sha256,
)
from friday.file_delivery import (
    AuthorizedFileBytes,
    CurrentMessageUploadBatchIdentity,
    CurrentMessageUploadFileIdentity,
)
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
from friday.organs.engineer.command import kernel as command_kernel_module
from friday.organs.engineer.command.boundary import MissingControllerBoundary, SystemdCgroupBoundary
from friday.organs.engineer.command.contracts import (
    AUTONOMOUS_DELEGATION_SCHEMA,
    COMMAND_GRANT_SCHEMA,
    COMMAND_GRANT_VERSION,
    MAX_TIMEOUT_SEC,
    OWNER_SOURCE_SCHEMA,
    sha256_bytes,
)
from friday.organs.engineer.command.inputs import command_input_descriptor, command_input_manifest
from friday.organs.engineer.command.resolve import attest_trusted_path, resolve_held, resolve_named
from friday.organs.engineer.command.spawn_helper import SpawnBroker
from friday.source_identity import authorized_file_snapshot_token

GRANT_SECRET = b"friday-engineer-command-kernel-tests-secret"
SOURCE_SECRET = b"friday-engineer-owner-source-tests-secret"
CONFIRM_SECRET = b"friday-engineer-owner-confirm-tests-secret"
ACTOR = "owner-1"
SOURCE_HASH = sha256_bytes(b"owner-turn-body")
SOURCE_STEP_ID = "ecstep-" + "1" * 32


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
    kwargs.setdefault("timeout_sec", 30)
    return CommandRequest(
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        argv=argv,
        idempotency_key=key,
        **kwargs,
    )


def _shell(command: str, key: str, **kwargs) -> CommandRequest:
    kwargs.setdefault("timeout_sec", 30)
    return CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.OWNER_TURN,
        shell_command=command,
        idempotency_key=key,
        **kwargs,
    )


def _host_shell(command: str, key: str, **kwargs) -> CommandRequest:
    return CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.MODEL,
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
        source_step_id=kwargs.get("source_step_id", SOURCE_STEP_ID),
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
    delivery_chat_id = kwargs.pop("delivery_chat_id", "")
    source_step_id = kwargs.pop(
        "source_step_id",
        "ecstep-" + sha256_bytes(request.idempotency_key.encode("utf-8"))[:32],
    )
    source = _attest(
        source_auth,
        request,
        isolation_profile=isolation,
        actor_id=actor_id,
        source_step_id=source_step_id,
    )
    confirmation = None
    if kwargs.pop("destructive", True):
        confirmation = _confirm(kernel, source, request)
    token = kernel.authority.issue(request, source=source, confirmation=confirmation)
    return kernel.submit(
        request,
        token,
        actor_id=actor_id,
        delivery_chat_id=delivery_chat_id,
    )


def _submit_host(kernel: CommandKernel, request: CommandRequest, **kwargs) -> str:
    source_step_id = kwargs.pop(
        "source_step_id",
        "ecstep-" + sha256_bytes(request.idempotency_key.encode("utf-8"))[:32],
    )
    source = kernel.authority.source_authority.attest(
        actor_id=kwargs.pop("actor_id", ACTOR),
        tenant_id=kwargs.pop("tenant_id", "tenant-1"),
        conversation_id=kwargs.pop("conversation_id", "conv-1"),
        channel="cli_test",
        source_row_id=kwargs.pop("source_row_id", "row-1"),
        source_step_id=source_step_id,
        source_hash=SOURCE_HASH,
        telegram_update_id=kwargs.pop("telegram_update_id", "upd-1"),
        isolation_profile=IsolationProfile.HOST_USER,
        idempotency_key=request.idempotency_key,
    )
    delegation = kernel.authority.source_authority.delegate_autonomous(
        source,
        expires_at=int(time.time()) + 60,
    )
    token = kernel.authority.issue_autonomous(
        request,
        source=source,
        delegation=delegation,
    )
    return kernel.submit(request, token, actor_id=source.actor_id, **kwargs)


def _wait(kernel: CommandKernel, job_id: str):
    return kernel.wait(job_id, actor_id=ACTOR)


def test_argv_echo_completes_without_inheriting_caller_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_terminal_result_is_conversation_scoped_and_returns_sealed_bytes(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf 'sealed-result' > output/result.bin", key=_key("terminal-result"))
    job_id = _submit(kernel, request, destructive=True)
    _wait(kernel, job_id)

    with pytest.raises(CommandError, match="conversation_mismatch"):
        kernel.terminal_result(
            job_id,
            actor_id=ACTOR,
            conversation_id="conv-other",
        )
    with pytest.raises(CommandError, match="conversation_required"):
        kernel.terminal_result(
            job_id,
            actor_id=ACTOR,
            conversation_id=None,  # type: ignore[arg-type]
        )

    receipt, outputs = kernel.terminal_result(
        job_id,
        actor_id=ACTOR,
        conversation_id="conv-1",
    )
    assert receipt.status is CommandStatus.COMPLETED
    assert outputs == ((receipt.generated_files[0], b"sealed-result"),)


def test_legacy_receipt_remains_status_readable_but_outputs_are_not_publishable(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf legacy > output/result.bin", key=_key("legacy-receipt"))
    job_id = _submit(kernel, request, destructive=True)
    receipt = _wait(kernel, job_id)
    legacy_payload = receipt.to_public_payload()
    legacy_payload.pop("generated_files_sha256")
    legacy_mac = kernel.authority.sign_receipt(legacy_payload)
    store_root = kernel.store.root
    with kernel.store.transaction():
        kernel.store.update_job(job_id, {"receipt_mac": legacy_mac})
    kernel.close()

    restarted = CommandKernel(store_root, _authority())
    legacy_receipt, mac_version = restarted.terminal_receipt(
        job_id,
        actor_id=ACTOR,
        conversation_id="conv-1",
    )
    assert legacy_receipt.status is CommandStatus.COMPLETED
    assert mac_version == 1
    with pytest.raises(CommandError, match="legacy_output_receipt_unpublishable"):
        restarted.terminal_result(
            job_id,
            actor_id=ACTOR,
            conversation_id="conv-1",
        )


def test_terminal_result_refuses_a_tampered_receipt_mac(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf result > output/result.bin", key=_key("receipt-mac"))
    job_id = _submit(kernel, request, destructive=True)
    _wait(kernel, job_id)
    store_root = kernel.store.root
    with kernel.store.transaction():
        kernel.store.update_job(job_id, {"receipt_mac": "0" * 64})
    kernel.close()

    restarted = CommandKernel(store_root, _authority())
    with pytest.raises(CommandError, match="corrupt_job_state"):
        restarted.terminal_result(
            job_id,
            actor_id=ACTOR,
            conversation_id="conv-1",
        )


def test_terminal_result_receipt_mac_binds_the_exact_output_inventory(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("printf result > output/result.bin", key=_key("inventory-mac"))
    job_id = _submit(kernel, request, destructive=True)
    receipt = _wait(kernel, job_id)
    descriptor = receipt.generated_files[0]
    forged_inventory = json.dumps(
        [
            {
                "mode": descriptor.mode ^ 0o100,
                "relative_path": descriptor.relative_path,
                "sha256": descriptor.sha256,
                "size_bytes": descriptor.size_bytes,
            }
        ],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    store_root = kernel.store.root
    with kernel.store.transaction():
        kernel.store.update_job(job_id, {"generated_files_json": forged_inventory})
    kernel.close()

    restarted = CommandKernel(store_root, _authority())
    with pytest.raises(CommandError, match="corrupt_job_state"):
        restarted.terminal_result(
            job_id,
            actor_id=ACTOR,
            conversation_id="conv-1",
        )


@pytest.mark.parametrize(
    "corrupt_fields",
    [
        {"status": "not-a-command-status"},
        {"started_at": "nan"},
    ],
)
def test_progress_normalizes_corrupt_durable_state(
    tmp_path: Path,
    corrupt_fields: dict[str, object],
) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("corrupt-progress"))
    job_id = _submit(kernel, request, destructive=True)
    _wait(kernel, job_id)
    with kernel.store.transaction():
        kernel.store.update_job(job_id, corrupt_fields)

    with pytest.raises(CommandError, match="corrupt_job_state"):
        kernel.progress(job_id, actor_id=ACTOR, conversation_id="conv-1")


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
    second = _submit(kernel, request)
    assert second == first


def test_idempotent_submit_preserves_exact_delivery_scope(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("delivery-idem"))
    first = _submit(kernel, request, delivery_chat_id="5001")
    _wait(kernel, first)
    assert _submit(kernel, request, delivery_chat_id="5001") == first
    with pytest.raises(CommandError, match="delivery_scope_mismatch"):
        _submit(kernel, request, delivery_chat_id="5002")


def test_idempotent_submit_does_not_refocus_an_older_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    first_request = _argv("/usr/bin/true", key=_key("focus-first"))
    first = _submit(kernel, first_request)
    _wait(kernel, first)
    second_request = _argv("/usr/bin/true", key=_key("focus-second"))
    second = _submit(kernel, second_request)
    _wait(kernel, second)

    assert (
        kernel.resolve_job_reference(
            None,
            actor_id=ACTOR,
            tenant_id="tenant-1",
            conversation_id="conv-1",
            channel="cli_test",
        )
        == second
    )
    assert _submit(kernel, first_request) == first
    assert (
        kernel.resolve_job_reference(
            None,
            actor_id=ACTOR,
            tenant_id="tenant-1",
            conversation_id="conv-1",
            channel="cli_test",
        )
        == second
    )


def test_cancel_reference_persists_intent_before_signalling_live_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/sleep", "20", key=_key("cancel-current"), timeout_sec=30)
    job_id = _submit(kernel, request)

    assert (
        kernel.cancel_reference(
            None,
            actor_id=ACTOR,
            tenant_id="tenant-1",
            conversation_id="conv-1",
            channel="cli_test",
        )
        == job_id
    )
    assert kernel.store.read_job(job_id)["cancel_requested_at"] is not None
    receipt = _wait(kernel, job_id)
    assert receipt.status is CommandStatus.CANCELLED


def test_idempotency_conflict_on_different_digest(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    key = _key("conflict")
    first_req = _argv("/usr/bin/true", key=key)
    _submit(kernel, first_req)
    second_req = _argv("/usr/bin/false", key=key)
    with pytest.raises(CommandError, match="idempotency_conflict"):
        _submit(kernel, second_req)


def test_committed_work_item_fence_wins_before_grant_parsing(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key="ecmd-" + "1" * 64)
    try:
        kernel.store.create_engineer_work_item_fence(
            actor_id=ACTOR,
            idempotency_key=request.idempotency_key,
            work_item_id="ewi_" + "2" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256="3" * 64,
            command_digest=request.digest,
            created_at=123.5,
        )
        with pytest.raises(CommandError, match="idempotency_fenced"):
            kernel.submit(request, "not-a-grant", actor_id=ACTOR)
        assert kernel.store.lookup_idempotency(ACTOR, request.idempotency_key) is None
    finally:
        kernel.close()


def test_in_flight_submit_is_fenced_between_both_admission_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key="ecmd-" + "4" * 64)
    source = _attest(kernel.authority.source_authority, request)
    confirmation = _confirm(kernel, source, request)
    token = kernel.authority.issue(request, source=source, confirmation=confirmation)
    between_lookups = threading.Event()
    continue_submit = threading.Event()
    original_resolve = command_kernel_module.resolve_request
    result: dict[str, object] = {}

    def _pause_between_lookups(*args, **kwargs):
        between_lookups.set()
        if not continue_submit.wait(timeout=5):
            raise AssertionError("fence race test did not resume")
        return original_resolve(*args, **kwargs)

    def _submit_in_flight() -> None:
        try:
            result["job_id"] = kernel.submit(request, token, actor_id=ACTOR)
        except BaseException as exc:  # test thread must return every failure to the parent
            result["error"] = exc

    monkeypatch.setattr(command_kernel_module, "resolve_request", _pause_between_lookups)
    worker = threading.Thread(target=_submit_in_flight, name="fence-race-submit")
    worker.start()
    try:
        assert between_lookups.wait(timeout=5)
        kernel.store.create_engineer_work_item_fence(
            actor_id=ACTOR,
            idempotency_key=request.idempotency_key,
            work_item_id="ewi_" + "5" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256="6" * 64,
            command_digest=request.digest,
            created_at=123.5,
        )
        continue_submit.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert "job_id" not in result
        error = result.get("error")
        assert isinstance(error, CommandError)
        assert error.code == "idempotency_fenced"
        assert kernel.store.lookup_idempotency(ACTOR, request.idempotency_key) is None
    finally:
        continue_submit.set()
        worker.join(timeout=5)
        kernel.close()


def test_in_flight_submit_is_fenced_by_same_source_with_a_different_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key="ecmd-" + "7" * 64)
    source = _attest(
        kernel.authority.source_authority,
        request,
        source_step_id="ecstep-" + "8" * 32,
    )
    confirmation = _confirm(kernel, source, request)
    token = kernel.authority.issue(request, source=source, confirmation=confirmation)
    source_values = {
        "owner_id": source.actor_id,
        "tenant_id": source.tenant_id,
        "conversation_id": source.conversation_id,
        "channel": source.channel,
        "source_row_id": source.source_row_id,
        "source_hash": source.source_hash,
        "telegram_update_id": source.telegram_update_id,
        "delivery_chat_id": "",
    }
    source_binding = canonical_engineer_source_binding_sha256(
        **source_values,
        source_step_id=source.source_step_id,
    )
    legacy_binding = legacy_engineer_source_binding_sha256(**source_values)
    after_first_lookup = threading.Event()
    continue_submit = threading.Event()
    original_resolve = command_kernel_module.resolve_request
    result: dict[str, object] = {}

    def _pause_after_first_lookup(*args, **kwargs):
        after_first_lookup.set()
        if not continue_submit.wait(timeout=5):
            raise AssertionError("source race test did not resume")
        return original_resolve(*args, **kwargs)

    def _submit_in_flight() -> None:
        try:
            result["job_id"] = kernel.submit(request, token, actor_id=ACTOR)
        except BaseException as exc:  # return every thread failure to the parent
            result["error"] = exc

    monkeypatch.setattr(command_kernel_module, "resolve_request", _pause_after_first_lookup)
    worker = threading.Thread(target=_submit_in_flight, name="source-slot-race-submit")
    worker.start()
    try:
        assert after_first_lookup.wait(timeout=5)
        kernel.store.create_engineer_work_item_fence(
            actor_id=ACTOR,
            idempotency_key="ecmd-" + "9" * 64,
            work_item_id="ewi_" + "a" * 32,
            expected_revision=1,
            step_ordinal=1,
            source_binding_sha256=source_binding,
            legacy_source_binding_sha256=legacy_binding,
            command_digest=request.digest,
            created_at=123.5,
        )
        continue_submit.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert "job_id" not in result
        error = result.get("error")
        assert isinstance(error, CommandError)
        assert error.code == "idempotency_fenced"
        assert kernel.store.lookup_idempotency(ACTOR, request.idempotency_key) is None
    finally:
        continue_submit.set()
        worker.join(timeout=5)
        kernel.close()


def test_lifecycle_is_rechecked_immediately_before_boundary_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("pre-spawn-lifecycle"))
    allocated = False

    def _not_ready() -> None:
        raise CommandError("command_store_lifecycle_unavailable")

    def _allocate(*args, **kwargs):
        nonlocal allocated
        allocated = True
        raise AssertionError("effect boundary was allocated before lifecycle readiness")

    monkeypatch.setattr(kernel.store, "assert_lifecycle_ready", _not_ready)
    monkeypatch.setattr(kernel.boundary, "allocate", _allocate)
    try:
        with pytest.raises(CommandError, match="command_store_lifecycle_unavailable"):
            _submit(kernel, request)
        assert allocated is False
        binding = kernel.store.lookup_idempotency_binding(ACTOR, request.idempotency_key)
        assert binding is not None
        assert kernel.store.read_job(str(binding["job_id"]))["status"] == "failed"
    finally:
        kernel.close()


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
        source_step_id=SOURCE_STEP_ID,
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
        with pytest.raises(
            CommandError, match="setid_refused|destructive_confirmation_required|symlink_refused"
        ):
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
    token = kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))
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


def test_output_hardlink_refusal_is_preserved(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell(
        "printf secret > output/a; ln output/a output/b",
        key=_key("hardlink-out"),
    )
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code == "output_hardlink_refused"
    assert receipt.generated_files == ()


def test_payload_cannot_forge_the_export_error_marker(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell(
        "printf output_symlink_refused > output/.friday-export-error.v1",
        key=_key("reserved-export-marker"),
    )
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code == "output_reserved_name"
    assert receipt.generated_files == ()


def test_payload_exit_125_is_not_mistaken_for_an_export_refusal(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _shell("exit 125", key=_key("payload-exit-125"))
    receipt = _wait(kernel, _submit(kernel, request, destructive=True))
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code == "nonzero_exit"
    assert receipt.exit_code == 125


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


def test_host_user_requires_explicit_autonomous_model_shell_delegation(tmp_path: Path) -> None:
    def clock() -> int:
        return 1_000

    kernel = _kernel(tmp_path, clock=clock)
    request = CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.MODEL,
        shell_command="true",
        timeout_sec=None,
        idempotency_key=_key("host-user"),
    )
    source = _attest(
        kernel.authority.source_authority,
        request,
        isolation_profile=IsolationProfile.HOST_USER,
    )
    with pytest.raises(CommandError, match="autonomous_delegation_required"):
        kernel.authority.issue(request, source=source)
    delegation = kernel.authority.source_authority.delegate_autonomous(
        source,
        expires_at=1_060,
    )
    token = kernel.authority.issue_autonomous(
        request,
        source=source,
        delegation=delegation,
    )
    assert source.identity_payload()["schema"] == OWNER_SOURCE_SCHEMA
    assert delegation.identity_payload()["schema"] == AUTONOMOUS_DELEGATION_SCHEMA
    assert kernel.authority.inspect(token)["schema"] == COMMAND_GRANT_SCHEMA
    assert kernel.authority.inspect(token)["v"] == COMMAND_GRANT_VERSION
    grant = kernel.authority.parse(token, request, actor_id=ACTOR)
    assert grant.isolation_profile is IsolationProfile.HOST_USER
    assert grant.origin is CommandOrigin.MODEL
    assert grant.lane is CommandLane.SHELL
    assert grant.autonomous_delegated is True
    assert grant.destructive_confirmed is False
    assert grant.autonomous_delegation_nonce == delegation.nonce
    assert grant.source_step_id == SOURCE_STEP_ID
    assert grant.expires_at == 1_060


def test_source_step_is_closed_and_old_or_incomplete_grants_fail_closed(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path, clock=lambda: 1_000)
    request = _host_shell("true", key=_key("source-step-contract"))
    for malformed in (
        "1" * 32,
        "ecstep-" + "A" * 32,
        "ecstep-" + "1" * 31,
        " ecstep-" + "1" * 32,
    ):
        with pytest.raises(CommandError, match="invalid_source_step"):
            _attest(
                kernel.authority.source_authority,
                request,
                isolation_profile=IsolationProfile.HOST_USER,
                source_step_id=malformed,
            )

    source = _attest(
        kernel.authority.source_authority,
        request,
        isolation_profile=IsolationProfile.HOST_USER,
    )
    delegation = kernel.authority.source_authority.delegate_autonomous(source, expires_at=1_060)
    token = kernel.authority.issue_autonomous(request, source=source, delegation=delegation)
    payload = kernel.authority.inspect(token)

    incomplete = dict(payload)
    incomplete.pop("source_step_id")
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.authority.parse(
            kernel.authority._encode(incomplete),  # noqa: SLF001 - signed malformed fixture
            request,
            actor_id=ACTOR,
        )

    legacy = dict(incomplete)
    legacy["schema"] = "friday.engineer.command.v4"
    legacy["v"] = 4
    with pytest.raises(CommandError, match="invalid_grant"):
        kernel.authority.parse(
            kernel.authority._encode(legacy),  # noqa: SLF001 - signed legacy fixture
            request,
            actor_id=ACTOR,
        )
    kernel.close()


def test_autonomous_delegation_rejects_forgery_and_exact_source_drift(tmp_path: Path) -> None:
    def clock() -> int:
        return 1_000

    kernel = _kernel(tmp_path, clock=clock)
    request = _host_shell("true", key=_key("delegation-integrity"))
    source = _attest(
        kernel.authority.source_authority,
        request,
        isolation_profile=IsolationProfile.HOST_USER,
    )
    delegation = kernel.authority.source_authority.delegate_autonomous(
        source,
        expires_at=1_060,
    )
    with pytest.raises(CommandError, match="invalid_autonomous_delegation"):
        kernel.authority.issue_autonomous(
            request,
            source=source,
            delegation=replace(delegation, mac="0" * 64),
        )
    for changed_source in (
        _attest(
            kernel.authority.source_authority,
            request,
            isolation_profile=IsolationProfile.HOST_USER,
            source_hash="d" * 64,
        ),
        kernel.authority.source_authority.attest(
            actor_id=source.actor_id,
            tenant_id=source.tenant_id,
            conversation_id=source.conversation_id,
            channel=source.channel,
            source_row_id="other-row",
            source_step_id=source.source_step_id,
            source_hash=source.source_hash,
            telegram_update_id=source.telegram_update_id,
            isolation_profile=IsolationProfile.HOST_USER,
            idempotency_key=source.idempotency_key,
        ),
        kernel.authority.source_authority.attest(
            actor_id=source.actor_id,
            tenant_id=source.tenant_id,
            conversation_id=source.conversation_id,
            channel=source.channel,
            source_row_id=source.source_row_id,
            source_step_id="ecstep-" + "2" * 32,
            source_hash=source.source_hash,
            telegram_update_id=source.telegram_update_id,
            isolation_profile=IsolationProfile.HOST_USER,
            idempotency_key=source.idempotency_key,
        ),
        kernel.authority.source_authority.attest(
            actor_id=source.actor_id,
            tenant_id=source.tenant_id,
            conversation_id=source.conversation_id,
            channel=source.channel,
            source_row_id=source.source_row_id,
            source_step_id=source.source_step_id,
            source_hash=source.source_hash,
            telegram_update_id=source.telegram_update_id,
            isolation_profile=IsolationProfile.HOST_USER,
            idempotency_key="different-idempotency",
        ),
    ):
        with pytest.raises(CommandError, match="autonomous_delegation_source_mismatch"):
            kernel.authority.issue_autonomous(
                request,
                source=changed_source,
                delegation=delegation,
            )
    kernel.close()


def test_autonomous_delegation_expiry_is_enforced_at_issue_and_later_use(tmp_path: Path) -> None:
    now = [1_000]

    def clock() -> int:
        return now[0]

    kernel = _kernel(tmp_path, clock=clock)
    request = _host_shell("true", key=_key("delegation-expiry"))
    source = _attest(
        kernel.authority.source_authority,
        request,
        isolation_profile=IsolationProfile.HOST_USER,
    )
    expired = kernel.authority.source_authority.delegate_autonomous(source, expires_at=999)
    with pytest.raises(CommandError, match="autonomous_delegation_expired"):
        kernel.authority.issue_autonomous(request, source=source, delegation=expired)
    delegation = kernel.authority.source_authority.delegate_autonomous(source, expires_at=1_060)
    token = kernel.authority.issue_autonomous(request, source=source, delegation=delegation)
    grant = kernel.authority.parse(token, request, actor_id=ACTOR)
    now[0] = 1_061
    with pytest.raises(CommandError, match="autonomous_delegation_expired"):
        kernel.authority.still_valid(grant)
    kernel.close()


def test_autonomous_issue_is_closed_to_model_shell_host_user_shape(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    shell = _host_shell("true", key=_key("host-shape"))
    source = _attest(
        kernel.authority.source_authority,
        shell,
        isolation_profile=IsolationProfile.HOST_USER,
    )
    delegation = kernel.authority.source_authority.delegate_autonomous(
        source,
        expires_at=int(time.time()) + 60,
    )
    argv_request = CommandRequest(
        lane=CommandLane.ARGV,
        origin=CommandOrigin.MODEL,
        argv=("/usr/bin/true",),
        idempotency_key=shell.idempotency_key,
    )
    with pytest.raises(CommandError, match="host_user_shell_required"):
        kernel.authority.issue_autonomous(
            argv_request,
            source=source,
            delegation=delegation,
        )
    owner_shell = CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.OWNER_TURN,
        shell_command="true",
        idempotency_key=shell.idempotency_key,
    )
    with pytest.raises(CommandError, match="autonomous_model_origin_required"):
        kernel.authority.issue_autonomous(
            owner_shell,
            source=source,
            delegation=delegation,
        )
    with pytest.raises(CommandError, match="autonomous_delegation_required"):
        kernel.authority.issue(shell, source=source)
    changed_request = _host_shell("printf changed", key="different-idempotency")
    with pytest.raises(CommandError, match="autonomous_delegation_source_mismatch"):
        kernel.authority.issue_autonomous(
            changed_request,
            source=kernel.authority.source_authority.attest(
                actor_id=source.actor_id,
                tenant_id=source.tenant_id,
                conversation_id=source.conversation_id,
                channel=source.channel,
                source_row_id=source.source_row_id,
                source_step_id=source.source_step_id,
                source_hash=source.source_hash,
                telegram_update_id=source.telegram_update_id,
                isolation_profile=IsolationProfile.HOST_USER,
                idempotency_key=changed_request.idempotency_key,
            ),
            delegation=delegation,
        )
    kernel.close()


def test_host_user_runs_direct_bash_with_host_environment_and_unbounded_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    probe = host_bin / "friday-host-probe"
    probe.write_text("#!/usr/bin/bash\nprintf host-path-ok", encoding="utf-8")
    probe.chmod(0o755)
    inherited_path = f"{host_bin}:{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", inherited_path)
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host-readable", encoding="utf-8")
    service_home = pwd.getpwuid(os.geteuid()).pw_dir
    network_namespace = os.readlink("/proc/self/ns/net")
    kernel = _kernel(tmp_path)
    command = "\n".join(
        (
            "set -eu",
            f'test "$HOME" = {shlex.quote(service_home)}',
            'test "$(pwd)" = "$FRIDAY_WORK_DIR"',
            'test "$PWD" = "$FRIDAY_WORK_DIR"',
            f"test -r {shlex.quote(str(secret))}",
            f'test "$(id -u)" = {os.geteuid()}',
            f'test "$(readlink /proc/self/ns/net)" = {shlex.quote(network_namespace)}',
            'test -d "$FRIDAY_JOB_DIR"',
            'test -d "$FRIDAY_WORK_DIR"',
            'test -d "$FRIDAY_INPUT_DIR"',
            'test -d "$FRIDAY_OUTPUT_DIR"',
            "friday-host-probe",
            'printf host-output > "$FRIDAY_OUTPUT_DIR/result.txt"',
        )
    )
    request = _host_shell(command, key=_key("host-direct"), timeout_sec=None)
    receipt = _wait(kernel, _submit_host(kernel, request))
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.exit_code == 0
    assert receipt.stdout == b"host-path-ok"
    assert receipt.isolation_profile is IsolationProfile.HOST_USER
    assert receipt.to_public_payload()["isolated"] is False
    assert receipt.input_manifest_sha256 == request.input_manifest_sha256
    assert receipt.generated_files[0].relative_path == "result.txt"
    assert (kernel.store.job_dir(receipt.job_id) / "sealed" / "result.txt").read_bytes() == b"host-output"
    job = kernel.store.read_job(receipt.job_id)
    assert int(job["timeout_sec"]) == 0
    assert int(job["host_user_authorized"]) == 1
    kernel.close()


def test_host_user_workbench_persists_across_steps_and_output_stays_per_job(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    first = _host_shell(
        'test "$(pwd)" = "$FRIDAY_WORK_DIR"; printf first > state.txt',
        key=_key("workbench-step-1"),
    )
    first_receipt = _wait(kernel, _submit_host(kernel, first))
    assert first_receipt.status is CommandStatus.COMPLETED
    assert first_receipt.generated_files == ()

    second = _host_shell(
        'test "$(pwd)" = "$FRIDAY_WORK_DIR"; printf -- \'-second\' >> state.txt; '
        'cp state.txt "$FRIDAY_OUTPUT_DIR/final.txt"',
        key=_key("workbench-step-2"),
    )
    second_receipt = _wait(kernel, _submit_host(kernel, second))
    assert second_receipt.status is CommandStatus.COMPLETED
    assert len(second_receipt.generated_files) == 1
    assert (
        kernel.store.job_dir(second_receipt.job_id) / "sealed" / "final.txt"
    ).read_bytes() == b"first-second"
    first_workbench = kernel.store.workbench_dir(
        actor_id=ACTOR,
        tenant_id="tenant-1",
        conversation_id="conv-1",
    )
    other_workbench = kernel.store.workbench_dir(
        actor_id=ACTOR,
        tenant_id="tenant-1",
        conversation_id="conv-other",
    )
    assert (first_workbench / "state.txt").read_bytes() == b"first-second"
    assert first_workbench != other_workbench
    assert not (other_workbench / "state.txt").exists()
    kernel.close()


def test_host_user_materializes_only_manifest_bound_reauthorized_inputs(tmp_path: Path) -> None:
    content = b"private-current-upload"
    content_digest = sha256_bytes(content)
    raw_id = "raw_0000000000000001"
    snapshot = authorized_file_snapshot_token(
        {
            "id": raw_id,
            "source": "telegram_upload",
            "source_ref": "file-1",
            "content_type": "text/plain",
            "received_at": "1",
            "content_hash": content_digest,
            "_raw_content": "",
            "_raw_metadata": "{}",
        },
        content_sha256=content_digest,
    )
    assert snapshot is not None
    identity_file = CurrentMessageUploadFileIdentity(
        raw_id=raw_id,
        source_identity_sha256=snapshot.source.identity_sha256,
        content_sha256=content_digest,
        size_bytes=len(content),
        filename="owner file.txt",
        mime_type="text/plain",
    )
    batch_identity = CurrentMessageUploadBatchIdentity(
        source_message_id="row-1",
        conversation_id="conv-1",
        source_message_identity_sha256="f" * 64,
        telegram_update_id="upd-1",
        uploaded_raw_ids=(raw_id,),
        files=(identity_file,),
    )
    authorized = AuthorizedFileBytes(
        raw_id=raw_id,
        filename=identity_file.filename,
        mime_type=identity_file.mime_type,
        content=content,
        snapshot_token=snapshot,
    )
    manifest = command_input_manifest(
        (
            command_input_descriptor(
                position=1,
                raw_id=raw_id,
                source_identity_sha256=identity_file.source_identity_sha256,
                content_sha256=identity_file.content_sha256,
                size_bytes=identity_file.size_bytes,
                original_filename=identity_file.filename,
                mime_type=identity_file.mime_type,
            ),
        )
    )
    kernel = _kernel(tmp_path)
    request = _host_shell(
        'cat "$FRIDAY_INPUT_DIR/01-owner-file.txt" > "$FRIDAY_OUTPUT_DIR/copied.txt"',
        key=_key("host-input"),
        input_manifest=manifest,
    )
    receipt = _wait(
        kernel,
        _submit_host(
            kernel,
            request,
            input_manifest=manifest,
            input_batch_identity=batch_identity,
            input_files=(authorized,),
        ),
    )
    assert receipt.status is CommandStatus.COMPLETED
    assert receipt.input_manifest_sha256 == manifest.canonical_sha256()
    assert receipt.to_public_payload()["input_manifest_sha256"] == manifest.canonical_sha256()
    assert (kernel.store.job_dir(receipt.job_id) / "sealed" / "copied.txt").read_bytes() == content
    materialized = kernel.store.job_dir(receipt.job_id) / "input" / "01-owner-file.txt"
    assert materialized.read_bytes() == content
    assert stat.S_IMODE(materialized.stat().st_mode) == 0o400
    kernel.close()


def test_host_user_rejects_private_input_byte_drift_before_admission(tmp_path: Path) -> None:
    content = b"authorized"
    raw_id = "raw_0000000000000002"
    digest = sha256_bytes(content)
    snapshot = authorized_file_snapshot_token(
        {
            "id": raw_id,
            "source": "telegram_upload",
            "source_ref": "file-2",
            "content_type": "text/plain",
            "received_at": "2",
            "content_hash": digest,
            "_raw_content": "",
            "_raw_metadata": "{}",
        },
        content_sha256=digest,
    )
    assert snapshot is not None
    identity_file = CurrentMessageUploadFileIdentity(
        raw_id=raw_id,
        source_identity_sha256=snapshot.source.identity_sha256,
        content_sha256=digest,
        size_bytes=len(content),
        filename="input.txt",
        mime_type="text/plain",
    )
    manifest = command_input_manifest(
        (
            command_input_descriptor(
                position=1,
                raw_id=raw_id,
                source_identity_sha256=identity_file.source_identity_sha256,
                content_sha256=digest,
                size_bytes=len(content),
                original_filename=identity_file.filename,
                mime_type=identity_file.mime_type,
            ),
        )
    )
    request = _host_shell("true", key=_key("host-input-drift"), input_manifest=manifest)
    identity = CurrentMessageUploadBatchIdentity(
        source_message_id="row-1",
        conversation_id="conv-1",
        source_message_identity_sha256="e" * 64,
        telegram_update_id="upd-1",
        uploaded_raw_ids=(raw_id,),
        files=(identity_file,),
    )
    changed = AuthorizedFileBytes(
        raw_id=raw_id,
        filename="input.txt",
        mime_type="text/plain",
        content=b"tampered!",
        snapshot_token=snapshot,
    )
    kernel = _kernel(tmp_path)
    with pytest.raises(CommandError, match="command_input_bytes_changed"):
        _submit_host(
            kernel,
            request,
            input_manifest=manifest,
            input_batch_identity=identity,
            input_files=(changed,),
        )
    assert kernel.store.lookup_idempotency(ACTOR, request.idempotency_key) is None
    assert list((kernel.store.root / "jobs").iterdir()) == []
    kernel.close()


def test_unbounded_host_user_job_remains_cancellable(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _host_shell(
        "exec /usr/bin/sleep 30",
        key=_key("host-cancel"),
        timeout_sec=None,
    )
    job_id = _submit_host(kernel, request)
    kernel.cancel(job_id, actor_id=ACTOR, conversation_id="conv-1")
    receipt = kernel.wait(
        job_id,
        actor_id=ACTOR,
        conversation_id="conv-1",
        timeout_sec=10,
    )
    assert receipt.status is CommandStatus.CANCELLED
    assert receipt.cancelled is True
    assert receipt.timed_out is False
    kernel.close()


def test_kernel_close_cancels_and_reaps_an_unbounded_host_job(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _host_shell(
        "exec /usr/bin/sleep 300",
        key=_key("host-shutdown"),
        timeout_sec=None,
    )
    job_id = _submit_host(kernel, request)
    running = kernel.store.read_job(job_id)
    cgroup = Path(str(running["cgroup_path"]))
    broker = kernel._broker._proc
    store_root = kernel.store.root

    started = time.monotonic()
    kernel.close(timeout_sec=30)
    assert time.monotonic() - started < 30
    assert broker.poll() is not None
    assert not cgroup.exists()

    # Acquiring the single-process store lease proves close released it; the
    # restarted kernel then verifies the shutdown result is durable, not merely
    # an in-memory cancellation flag.
    restarted = CommandKernel(store_root, _authority())
    receipt = restarted.wait(
        job_id,
        actor_id=ACTOR,
        conversation_id="conv-1",
        timeout_sec=0.1,
    )
    job = restarted.store.read_job(job_id)
    assert receipt.status is CommandStatus.CANCELLED
    assert receipt.cancelled is True
    assert job["cancel_requested_at"] is not None
    assert job["cleanup_pending"] == 0
    restarted.close()


def test_submit_crossing_close_is_admitted_then_cancelled_before_store_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _kernel(tmp_path)
    request = _host_shell(
        "exec /usr/bin/sleep 300",
        key=_key("submit-close-race"),
        timeout_sec=None,
    )
    source = kernel.authority.source_authority.attest(
        actor_id=ACTOR,
        tenant_id="tenant-1",
        conversation_id="conv-1",
        channel="cli_test",
        source_row_id="row-race",
        source_step_id=SOURCE_STEP_ID,
        source_hash=SOURCE_HASH,
        telegram_update_id="upd-race",
        isolation_profile=IsolationProfile.HOST_USER,
        idempotency_key=request.idempotency_key,
    )
    delegation = kernel.authority.source_authority.delegate_autonomous(
        source,
        expires_at=int(time.time()) + 60,
    )
    grant = kernel.authority.issue_autonomous(request, source=source, delegation=delegation)
    entered = threading.Event()
    release = threading.Event()
    submitted: list[str] = []
    failures: list[BaseException] = []
    original_submit = kernel._submit

    def delayed_submit(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test_submit_release_timeout")
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(kernel, "_submit", delayed_submit)

    def submitter() -> None:
        try:
            submitted.append(kernel.submit(request, grant, actor_id=ACTOR))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def closer() -> None:
        try:
            kernel.close(timeout_sec=30)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    submit_thread = threading.Thread(target=submitter)
    close_thread = threading.Thread(target=closer)
    submit_thread.start()
    assert entered.wait(timeout=5)
    close_thread.start()
    assert kernel._closing.wait(timeout=5)
    assert close_thread.is_alive()
    with pytest.raises(CommandError, match="command_kernel_closing"):
        kernel.submit(request, grant, actor_id=ACTOR)
    release.set()
    submit_thread.join(timeout=30)
    close_thread.join(timeout=30)

    assert not submit_thread.is_alive()
    assert not close_thread.is_alive()
    assert failures == []
    assert len(submitted) == 1
    store_root = kernel.store.root
    restarted = CommandKernel(store_root, _authority())
    receipt = restarted.wait(
        submitted[0],
        actor_id=ACTOR,
        conversation_id="conv-1",
        timeout_sec=0.1,
    )
    assert receipt.status is CommandStatus.CANCELLED
    restarted.close()


def test_kernel_close_is_idempotent_and_keeps_admission_closed(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    broker = kernel._broker._proc
    kernel.close()
    kernel.close()

    assert broker.poll() is not None
    request = _host_shell("true", key=_key("closed-submit"), timeout_sec=None)
    with pytest.raises(CommandError, match="command_kernel_closing"):
        kernel.submit(request, "unused", actor_id=ACTOR)


def test_unbounded_timeout_is_reserved_for_autonomous_host_user(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    request = _argv("/usr/bin/true", key=_key("isolated-unbounded"), timeout_sec=None)
    source = _attest(kernel.authority.source_authority, request)
    with pytest.raises(CommandError, match="isolated_timeout_required"):
        kernel.authority.issue(request, source=source, confirmation=_confirm(kernel, source, request))


def test_command_request_has_no_implicit_runtime_ceiling() -> None:
    request = CommandRequest(
        lane=CommandLane.SHELL,
        origin=CommandOrigin.MODEL,
        shell_command="true",
        idempotency_key=_key("default-unbounded"),
    )
    assert request.timeout_sec is None
    assert replace(request, timeout_sec=7 * 24 * 60 * 60).timeout_sec == 604_800
    assert replace(request, timeout_sec=MAX_TIMEOUT_SEC).timeout_sec == 2**31 - 1
    with pytest.raises(CommandError, match="invalid_request"):
        replace(request, timeout_sec=MAX_TIMEOUT_SEC + 1)


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
        _submit(kernel, request, delivery_chat_id="5001")
    row = kernel.store._conn.execute("SELECT job_id,receipt_mac FROM jobs").fetchone()  # noqa: SLF001
    assert row is not None and str(row["receipt_mac"])
    job_id = str(row["job_id"])
    store_root = kernel.store.root
    kernel.close()
    restarted = CommandKernel(
        store_root,
        _authority(),
        boundary=MissingControllerBoundary(),
    )
    receipt, mac_version = restarted.terminal_receipt(
        job_id,
        actor_id=ACTOR,
        conversation_id="conv-1",
    )
    assert receipt.status is CommandStatus.FAILED
    assert receipt.error_code == "resource_boundary_unproven"
    assert mac_version == 2


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
        "from pathlib import Path\np=Path('output')\nfor i in range(200):\n (p/f'f{i}').write_text('n')\n",
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
