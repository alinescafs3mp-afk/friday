"""Universal Engineer command kernel. Descriptive library seam, not conversational wiring."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
import weakref
from pathlib import Path

from .boundary import ProvenScope, ResourceBoundary, SystemdCgroupBoundary
from .contracts import (
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandProgress,
    CommandReceipt,
    CommandRequest,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
    ResolvedExecutable,
    ResourceLimits,
    TrustedPathContract,
    sha256_bytes,
)
from .grant import CommandGrantAuthority
from .isolate import require_profile
from .resolve import attest_trusted_path, resolve_bwrap, resolve_request
from .runner import SpawnedCommand, _pid_starttime
from .spawn_helper import SpawnBroker
from .store import CommandJobStore, decode_json_list
from .workspace import JobWorkspace


def _close_fds(fds: tuple[int, ...]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def _pid_alive_matching(pid: int | None, starttime: int | None) -> bool:
    if pid is None or int(pid) <= 0:
        return False
    observed = _pid_starttime(int(pid))
    if observed is None:
        return False
    if starttime is None:
        return False
    return int(observed) == int(starttime)


def _resolved_from_json(raw: str | None) -> ResolvedExecutable | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return ResolvedExecutable(
            requested=str(payload.get("requested") or ""),
            canonical_path=str(payload.get("canonical_path") or ""),
            owner_uid=int(payload["owner_uid"]),
            owner_gid=int(payload["owner_gid"]),
            mode=int(payload["mode"]),
            device=int(payload["device"]),
            inode=int(payload["inode"]),
            size_bytes=int(payload["size_bytes"]),
            mtime_ns=int(payload["mtime_ns"]),
            sha256=str(payload["sha256"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _files_from_json(raw: str | None) -> tuple[GeneratedFile, ...]:
    files: list[GeneratedFile] = []
    for item in decode_json_list(raw):
        files.append(
            GeneratedFile(
                relative_path=str(item.get("relative_path") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
                sha256=str(item.get("sha256") or ""),
                mode=int(item.get("mode") or 0),
            )
        )
    return tuple(files)


def _executable_json(resolved: ResolvedExecutable) -> str:
    return json.dumps(
        {
            "canonical_path": resolved.canonical_path,
            "device": resolved.device,
            "inode": resolved.inode,
            "mode": resolved.mode,
            "mtime_ns": resolved.mtime_ns,
            "owner_gid": resolved.owner_gid,
            "owner_uid": resolved.owner_uid,
            "requested": resolved.requested,
            "sha256": resolved.sha256,
            "size_bytes": resolved.size_bytes,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


class CommandKernel:
    def __init__(
        self,
        store_root: Path,
        authority: CommandGrantAuthority,
        *,
        trusted_path: TrustedPathContract | None = None,
        limits: ResourceLimits | None = None,
        boundary: ResourceBoundary | None = None,
    ) -> None:
        self.store = CommandJobStore(Path(store_root))
        self._store_finalizer = weakref.finalize(self, self.store.close)
        self.authority = authority
        self.authority.bind_store(self.store)
        self.trusted_path = trusted_path or TrustedPathContract.default()
        self.path_roots = attest_trusted_path(self.trusted_path)
        self.limits = limits or ResourceLimits.default()
        self.boundary = boundary if boundary is not None else SystemdCgroupBoundary()
        self._lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._live: dict[str, SpawnedCommand] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._receipts: dict[str, CommandReceipt] = {}
        self._broker = SpawnBroker()
        self._finalizer = weakref.finalize(self, self._broker.close)
        self._path_finalizer = weakref.finalize(
            self, _close_fds, tuple(root.dir_fd for root in self.path_roots)
        )
        self._reconcile_stale()

    def close(self) -> None:
        with self._lock:
            if self._live or any(worker.is_alive() for worker in self._threads.values()):
                raise CommandError("job_running")
        self._finalizer()
        self._path_finalizer()
        self._store_finalizer()

    def _reconcile_stale(self) -> None:
        for job in self.store.list_unreaped():
            job_id = str(job["job_id"])
            unit = str(job.get("systemd_unit") or "")
            cgroup_path = str(job.get("cgroup_path") or "")
            cleanup_proven = False
            if unit and cgroup_path:
                try:
                    scope = self.boundary.recover_scope(
                        job_id,
                        unit,
                        cgroup_path,
                        self.limits,
                        timeout_sec=int(job.get("timeout_sec") or 0),
                    )
                except CommandError:
                    scope = None
                if scope is not None:
                    cleanup_proven = bool(self.boundary.stop(scope))
            with self.store.transaction():
                self.store.update_job(
                    job_id,
                    {
                        "status": CommandStatus.UNKNOWN.value,
                        "error_code": "unknown_after_restart",
                        "finished_at": time.time(),
                        "cleanup_pending": 1 if unit and cgroup_path and not cleanup_proven else 0,
                    },
                )

    def _spawn_in_durable_scope(
        self,
        job_id: str,
        request: CommandRequest,
        workspace: JobWorkspace,
        spawned: SpawnedCommand,
        held,
        bwrap,
        scope: ProvenScope,
    ) -> None:
        """Persist the recoverable scope identity before the helper can release GO."""
        spawned.scope = scope
        try:
            with self.store.transaction():
                self.store.update_job(
                    job_id,
                    {
                        "cgroup_path": str(scope.cgroup),
                        "systemd_unit": scope.unit,
                        "cleanup_pending": 1,
                    },
                )
        except Exception as exc:
            spawned.abort()
            if isinstance(exc, CommandError):
                raise
            raise CommandError("durable_write_failed") from exc
        spawned.spawn(
            held,
            stdin=request.stdin,
            env=workspace.env(
                path_value=self.trusted_path.runtime_path,
                isolated=True,
            ),
            path_roots=self.path_roots,
            bwrap=bwrap,
            scope=scope,
            broker=self._broker,
        )

    def submit(self, request: CommandRequest, grant_token: str, *, actor_id: str) -> str:
        grant = None
        held = None
        bwrap = None
        try:
            with self.store.transaction():
                existing = self.store.lookup_idempotency(actor_id, request.idempotency_key)
                if existing is not None:
                    if existing["digest"] != request.digest:
                        raise CommandError("idempotency_conflict")
                    return existing["job_id"]
            grant = self.authority.parse(grant_token, request, actor_id=actor_id)
            require_profile(grant.isolation_profile)
            held = resolve_request(
                request,
                grant,
                trusted_path=self.trusted_path,
                path_roots=self.path_roots,
            )
            bwrap = resolve_bwrap()
            job_id = secrets.token_hex(16)
            job_dir = self.store.job_dir(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(job_dir, 0o700)
            workspace = JobWorkspace(job_dir)
            workspace.materialize()
            with self.store.transaction():
                existing = self.store.lookup_idempotency(actor_id, request.idempotency_key)
                if existing is not None:
                    if existing["digest"] != request.digest:
                        raise CommandError("idempotency_conflict")
                    return existing["job_id"]
                self.authority.still_valid(grant)
                self.store.consume_nonce(grant.nonce, exp=grant.expires_at, now=int(time.time()))
                if grant.confirmation_nonce:
                    self.store.consume_nonce(
                        grant.confirmation_nonce,
                        exp=grant.expires_at,
                        now=int(time.time()),
                    )
                self.store.insert_job(
                    {
                        "job_id": job_id,
                        "actor_id": grant.actor_id,
                        "tenant_id": grant.tenant_id,
                        "conversation_id": grant.conversation_id,
                        "channel": grant.channel,
                        "source_row_id": grant.source_row_id,
                        "source_hash": grant.source_hash,
                        "telegram_update_id": grant.telegram_update_id,
                        "isolation_profile": grant.isolation_profile.value,
                        "host_user_authorized": False,
                        "idempotency_key": grant.idempotency_key,
                        "command_digest": request.digest,
                        "argv_sha256": request.argv_sha256,
                        "lane": request.lane.value,
                        "origin": request.origin.value,
                        "status": CommandStatus.ADMITTED.value,
                        "grant_nonce": grant.nonce,
                        "timeout_sec": request.timeout_sec,
                        "max_stdout_bytes": request.max_stdout_bytes,
                        "max_stderr_bytes": request.max_stderr_bytes,
                        "created_at": time.time(),
                        "executable_json": _executable_json(held.resolved),
                    }
                )
            spawned = SpawnedCommand(
                workspace=workspace,
                timeout_sec=request.timeout_sec,
                max_stdout_bytes=request.max_stdout_bytes,
                max_stderr_bytes=request.max_stderr_bytes,
                isolation=grant.isolation_profile,
                limits=self.limits,
            )
            scope = None
            try:
                self.authority.still_valid(grant)
                with self._spawn_lock:
                    scope = self.boundary.allocate(job_id, self.limits, timeout_sec=request.timeout_sec)
                    self._spawn_in_durable_scope(
                        job_id,
                        request,
                        workspace,
                        spawned,
                        held,
                        bwrap,
                        scope,
                    )
            except CommandError as exc:
                spawned.abort()
                with self.store.transaction():
                    self.store.update_job(
                        job_id,
                        {
                            "status": CommandStatus.FAILED.value
                            if not spawned.effect_boundary_crossed
                            else CommandStatus.UNKNOWN.value,
                            "error_code": exc.code,
                            "finished_at": time.time(),
                            "effect_boundary_crossed": 1 if spawned.effect_boundary_crossed else 0,
                            "systemd_unit": scope.unit if scope is not None else None,
                            "cgroup_path": str(scope.cgroup) if scope is not None else None,
                            "cleanup_pending": 0 if scope is None or spawned.tree_empty else 1,
                        },
                    )
                receipt = self._receipt_from_spawned(
                    job_id,
                    request,
                    grant.isolation_profile,
                    grant.source_hash,
                    held.resolved,
                    spawned,
                    error_code=exc.code,
                    status=CommandStatus.UNKNOWN if spawned.effect_boundary_crossed else CommandStatus.FAILED,
                )
                self._receipts[job_id] = receipt
                raise
            try:
                with self.store.transaction():
                    self.store.update_job(
                        job_id,
                        {
                            "status": CommandStatus.RUNNING.value,
                            "pid": spawned.pid,
                            "pid_starttime": spawned.pid_starttime,
                            "cgroup_path": str(scope.cgroup) if scope is not None else None,
                            "systemd_unit": scope.unit if scope is not None else None,
                            "started_at": spawned.started_at,
                            "effect_boundary_crossed": 1,
                            "cleanup_pending": 1,
                        },
                    )
            except CommandError as persist_exc:
                spawned.abort()
                with contextlib.suppress(CommandError), self.store.transaction():
                    self.store.update_job(
                        job_id,
                        {
                            "status": CommandStatus.UNKNOWN.value,
                            "error_code": "unknown_after_spawn",
                            "finished_at": time.time(),
                            "effect_boundary_crossed": 1,
                            "cleanup_pending": 0 if spawned.tree_empty else 1,
                        },
                    )
                raise CommandError("unknown_after_spawn") from persist_exc
            with self._lock:
                self._live[job_id] = spawned
            worker = threading.Thread(
                target=self._reap,
                args=(job_id, request, grant, held, bwrap, spawned),
                name=f"engineer-command-{job_id[:8]}",
                daemon=False,
            )
            with self._lock:
                self._threads[job_id] = worker
            try:
                worker.start()
            except Exception as exc:
                spawned.abort()
                with self._lock:
                    self._live.pop(job_id, None)
                    self._threads.pop(job_id, None)
                with contextlib.suppress(CommandError), self.store.transaction():
                    self.store.update_job(
                        job_id,
                        {
                            "status": CommandStatus.UNKNOWN.value,
                            "error_code": "unknown_after_spawn",
                            "finished_at": time.time(),
                            "effect_boundary_crossed": 1,
                            "cleanup_pending": 0 if spawned.tree_empty else 1,
                        },
                    )
                receipt = self._receipt_from_spawned(
                    job_id,
                    request,
                    grant.isolation_profile,
                    grant.source_hash,
                    held.resolved,
                    spawned,
                    error_code="unknown_after_spawn",
                    status=CommandStatus.UNKNOWN,
                )
                self._receipts[job_id] = receipt
                raise CommandError("unknown_after_spawn") from exc
            held = None
            bwrap = None
            return job_id
        finally:
            if held is not None:
                held.close()
            if bwrap is not None:
                bwrap.close()

    def progress(self, job_id: str, *, actor_id: str) -> CommandProgress:
        self._require_actor(job_id, actor_id)
        with self._lock:
            live = self._live.get(job_id)
        if live is not None:
            elapsed = time.time() - (live.started_at or time.time())
            return CommandProgress(
                job_id=job_id,
                status=CommandStatus.RUNNING,
                elapsed_sec=max(0.0, elapsed),
                stdout_bytes=live.stdout_bytes,
                stderr_bytes=live.stderr_bytes,
                output_activity=live.output_activity,
                isolation_profile=live.isolation,
            )
        job = self.store.read_job(job_id)
        status = CommandStatus(str(job.get("status") or CommandStatus.UNKNOWN.value))
        isolation = IsolationProfile(
            str(job.get("isolation_profile") or IsolationProfile.ISOLATED_WORKSPACE.value)
        )
        if status in {CommandStatus.RUNNING, CommandStatus.ADMITTED}:
            if _pid_alive_matching(job.get("pid"), job.get("pid_starttime")):
                status = CommandStatus.UNKNOWN
            else:
                status = CommandStatus.UNKNOWN
            with self.store.transaction():
                self.store.update_job(
                    job_id,
                    {
                        "status": status.value,
                        "error_code": "unknown_after_restart",
                    },
                )
        started = float(job.get("started_at") or time.time())
        finished = job.get("finished_at")
        elapsed = float(finished or time.time()) - started
        workspace = JobWorkspace(self.store.job_dir(job_id))
        stdout_bytes = workspace.stdout_path.stat().st_size if workspace.stdout_path.exists() else 0
        stderr_bytes = workspace.stderr_path.stat().st_size if workspace.stderr_path.exists() else 0
        return CommandProgress(
            job_id=job_id,
            status=status,
            elapsed_sec=max(0.0, elapsed),
            stdout_bytes=int(stdout_bytes),
            stderr_bytes=int(stderr_bytes),
            output_activity=bool(stdout_bytes or stderr_bytes),
            isolation_profile=isolation,
        )

    def cancel(self, job_id: str, *, actor_id: str) -> None:
        self._require_actor(job_id, actor_id)
        with self._lock:
            live = self._live.get(job_id)
        if live is None:
            raise CommandError("job_not_running")
        live.request_cancel()

    def wait(self, job_id: str, *, actor_id: str, timeout_sec: float | None = None) -> CommandReceipt:
        self._require_actor(job_id, actor_id)
        deadline = time.monotonic() + (float(timeout_sec) if timeout_sec is not None else 3600.0)
        while True:
            with self._lock:
                receipt = self._receipts.get(job_id)
                worker = self._threads.get(job_id)
            if receipt is not None:
                return receipt
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError("wait_timeout")
            if worker is not None:
                worker.join(timeout=remaining)
                with self._lock:
                    receipt = self._receipts.get(job_id)
                if receipt is not None:
                    return receipt
                raise CommandError("wait_timeout")
            job = self.store.read_job(job_id)
            status = CommandStatus(str(job.get("status") or CommandStatus.UNKNOWN.value))
            if status not in {CommandStatus.ADMITTED, CommandStatus.RUNNING, CommandStatus.PLANNED}:
                return self._receipt_from_job(job_id)
            time.sleep(min(0.02, max(0.0, remaining)))

    def _require_actor(self, job_id: str, actor_id: str) -> None:
        job = self.store.read_job(job_id)
        if str(job.get("actor_id") or "") != actor_id:
            raise CommandError("actor_mismatch")

    def _reap(
        self, job_id: str, request: CommandRequest, grant, held, bwrap, spawned: SpawnedCommand
    ) -> None:
        error_code = ""
        status = CommandStatus.FAILED
        generated: tuple[GeneratedFile, ...] = ()
        resolved = held.resolved
        try:
            spawned.wait()
            try:
                generated = spawned.workspace.admit_generated_files()
            except CommandError as exc:
                error_code = exc.code
                status = CommandStatus.FAILED
            else:
                if spawned.quota_exceeded:
                    status = CommandStatus.FAILED
                    error_code = spawned.quota_code or "output_quota_exceeded"
                elif not spawned.eof_proven or not spawned.tree_empty:
                    status = CommandStatus.UNKNOWN
                    error_code = "tree_or_eof_unproven"
                elif spawned.timed_out:
                    status = CommandStatus.TIMEOUT
                    error_code = "timeout"
                elif spawned.cancelled:
                    status = CommandStatus.CANCELLED
                    error_code = "cancelled"
                elif spawned.exit_code == 0:
                    status = CommandStatus.COMPLETED
                    error_code = ""
                else:
                    status = CommandStatus.FAILED
                    error_code = "nonzero_exit"
        except CommandError as exc:
            spawned.abort()
            error_code = exc.code
            status = CommandStatus.UNKNOWN if spawned.effect_boundary_crossed else CommandStatus.FAILED
        except Exception:
            spawned.abort()
            error_code = "reap_failed"
            status = CommandStatus.UNKNOWN
        finally:
            spawned.close_pidfd()
            if held is not None:
                held.close()
            if bwrap is not None:
                bwrap.close()
        receipt = self._receipt_from_spawned(
            job_id,
            request,
            grant.isolation_profile,
            grant.source_hash,
            resolved,
            spawned,
            error_code=error_code,
            status=status,
            generated=generated,
        )
        generated_json = json.dumps(
            [
                {
                    "mode": item.mode,
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in receipt.generated_files
            ],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

        def _fields(value: CommandReceipt) -> dict[str, object]:
            return {
                "cancelled": 1 if value.cancelled else 0,
                "error_code": value.error_code,
                "exit_code": value.exit_code,
                "finished_at": value.finished_at,
                "generated_files_json": generated_json,
                "receipt_mac": value.receipt_mac,
                "signal": value.signal,
                "status": value.status.value,
                "stderr_sha256": value.stderr_sha256,
                "stdout_sha256": value.stdout_sha256,
                "timed_out": 1 if value.timed_out else 0,
                "truncated_stderr": 1 if value.truncated_stderr else 0,
                "truncated_stdout": 1 if value.truncated_stdout else 0,
                "effect_boundary_crossed": 1 if value.effect_boundary_crossed else 0,
                "cleanup_pending": 0 if spawned.tree_empty else 1,
            }

        try:
            with self.store.transaction():
                self.store.update_job(job_id, _fields(receipt))
        except Exception:
            spawned.abort()
            receipt = self._receipt_from_spawned(
                job_id,
                request,
                grant.isolation_profile,
                grant.source_hash,
                resolved,
                spawned,
                error_code="final_receipt_persist_failed",
                status=CommandStatus.UNKNOWN,
                generated=generated,
            )
            # A transient first commit failure may still permit a durable
            # UNKNOWN marker. If storage remains unavailable, the in-memory
            # receipt below unblocks waiters and restart reconciliation sees
            # the earlier RUNNING record.
            with contextlib.suppress(Exception), self.store.transaction():
                self.store.update_job(job_id, _fields(receipt))
        finally:
            with self._lock:
                self._receipts[job_id] = receipt
                self._live.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _receipt_from_spawned(
        self,
        job_id: str,
        request: CommandRequest,
        isolation: IsolationProfile,
        source_hash: str,
        resolved: ResolvedExecutable,
        spawned: SpawnedCommand,
        *,
        error_code: str,
        status: CommandStatus,
        generated: tuple[GeneratedFile, ...] = (),
    ) -> CommandReceipt:
        receipt = CommandReceipt(
            job_id=job_id,
            status=status,
            lane=request.lane,
            origin=request.origin,
            isolation_profile=isolation,
            command_digest=request.digest,
            argv_sha256=request.argv_sha256,
            source_hash=source_hash,
            exit_code=spawned.exit_code,
            signal=spawned.signal_num,
            timed_out=spawned.timed_out,
            cancelled=spawned.cancelled,
            truncated_stdout=spawned.truncated_stdout,
            truncated_stderr=spawned.truncated_stderr,
            started_at=spawned.started_at,
            finished_at=spawned.finished_at,
            executable=resolved,
            stdout_sha256=sha256_bytes(spawned.stdout),
            stderr_sha256=sha256_bytes(spawned.stderr),
            stdout=spawned.stdout,
            stderr=spawned.stderr,
            generated_files=generated,
            error_code=error_code,
            effect_boundary_crossed=spawned.effect_boundary_crossed,
            receipt_mac="",
            shell_subcommands_attested=False,
        )
        mac = self.authority.sign_receipt(receipt.to_public_payload())
        return CommandReceipt(
            job_id=receipt.job_id,
            status=receipt.status,
            lane=receipt.lane,
            origin=receipt.origin,
            isolation_profile=receipt.isolation_profile,
            command_digest=receipt.command_digest,
            argv_sha256=receipt.argv_sha256,
            source_hash=receipt.source_hash,
            exit_code=receipt.exit_code,
            signal=receipt.signal,
            timed_out=receipt.timed_out,
            cancelled=receipt.cancelled,
            truncated_stdout=receipt.truncated_stdout,
            truncated_stderr=receipt.truncated_stderr,
            started_at=receipt.started_at,
            finished_at=receipt.finished_at,
            executable=receipt.executable,
            stdout_sha256=receipt.stdout_sha256,
            stderr_sha256=receipt.stderr_sha256,
            stdout=receipt.stdout,
            stderr=receipt.stderr,
            generated_files=receipt.generated_files,
            error_code=receipt.error_code,
            effect_boundary_crossed=receipt.effect_boundary_crossed,
            receipt_mac=mac,
            shell_subcommands_attested=False,
        )

    def _receipt_from_job(self, job_id: str) -> CommandReceipt:
        job = self.store.read_job(job_id)
        status = CommandStatus(str(job.get("status") or CommandStatus.UNKNOWN.value))
        if status in {CommandStatus.RUNNING, CommandStatus.ADMITTED}:
            status = CommandStatus.UNKNOWN
            with self.store.transaction():
                self.store.update_job(
                    job_id,
                    {"status": status.value, "error_code": "unknown_after_restart"},
                )
            job = self.store.read_job(job_id)
        workspace = JobWorkspace(self.store.job_dir(job_id))
        stdout_sha = str(job.get("stdout_sha256") or "")
        stderr_sha = str(job.get("stderr_sha256") or "")
        try:
            stdout = (
                workspace.read_evidence_verified(
                    "stdout.bin",
                    expected_sha256=stdout_sha,
                    cap=int(job.get("max_stdout_bytes") or MAX_STDOUT_BYTES),
                )
                if stdout_sha
                else b""
            )
            stderr = (
                workspace.read_evidence_verified(
                    "stderr.bin",
                    expected_sha256=stderr_sha,
                    cap=int(job.get("max_stderr_bytes") or MAX_STDERR_BYTES),
                )
                if stderr_sha
                else b""
            )
        except CommandError:
            status = CommandStatus.UNKNOWN
            stdout = b""
            stderr = b""
            with self.store.transaction():
                self.store.update_job(
                    job_id,
                    {"status": status.value, "error_code": "corrupt_evidence"},
                )
            job = self.store.read_job(job_id)
        isolation = IsolationProfile(
            str(job.get("isolation_profile") or IsolationProfile.ISOLATED_WORKSPACE.value)
        )
        receipt = CommandReceipt(
            job_id=job_id,
            status=status,
            lane=CommandLane(str(job.get("lane") or CommandLane.ARGV.value)),
            origin=CommandOrigin(str(job.get("origin") or CommandOrigin.OWNER_TURN.value)),
            isolation_profile=isolation,
            command_digest=str(job.get("command_digest") or ""),
            argv_sha256=str(job.get("argv_sha256") or ""),
            source_hash=str(job.get("source_hash") or ""),
            exit_code=job.get("exit_code"),
            signal=job.get("signal"),
            timed_out=bool(job.get("timed_out")),
            cancelled=bool(job.get("cancelled")),
            truncated_stdout=bool(job.get("truncated_stdout")),
            truncated_stderr=bool(job.get("truncated_stderr")),
            started_at=float(job.get("started_at") or 0.0),
            finished_at=job.get("finished_at"),
            executable=_resolved_from_json(job.get("executable_json")),
            stdout_sha256=str(job.get("stdout_sha256") or sha256_bytes(stdout)),
            stderr_sha256=str(job.get("stderr_sha256") or sha256_bytes(stderr)),
            stdout=stdout,
            stderr=stderr,
            generated_files=_files_from_json(job.get("generated_files_json")),
            error_code=str(job.get("error_code") or ""),
            effect_boundary_crossed=bool(job.get("effect_boundary_crossed")),
            receipt_mac=str(job.get("receipt_mac") or ""),
            shell_subcommands_attested=False,
        )
        return receipt
