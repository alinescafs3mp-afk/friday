"""Universal Engineer command kernel. Descriptive library seam, not conversational wiring."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

from .contracts import (
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandProgress,
    CommandReceipt,
    CommandRequest,
    CommandStatus,
    GeneratedFile,
    ResolvedExecutable,
    sha256_bytes,
)
from .grant import CommandGrantAuthority
from .resolve import resolve_request
from .runner import SpawnedCommand
from .store import CommandJobStore, atomic_write_json
from .workspace import JobWorkspace


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _resolved_from_state(payload: dict) -> ResolvedExecutable | None:
    raw = payload.get("executable")
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        return ResolvedExecutable(
            requested=str(raw.get("requested") or ""),
            canonical_path=str(raw.get("canonical_path") or ""),
            owner_uid=int(raw["owner_uid"]),
            owner_gid=int(raw["owner_gid"]),
            mode=int(raw["mode"]),
            device=int(raw["device"]),
            inode=int(raw["inode"]),
            size_bytes=int(raw["size_bytes"]),
            mtime_ns=int(raw["mtime_ns"]),
            sha256=str(raw["sha256"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _files_from_state(payload: dict) -> tuple[GeneratedFile, ...]:
    items = payload.get("generated_files") or []
    if not isinstance(items, list):
        return ()
    files: list[GeneratedFile] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        files.append(
            GeneratedFile(
                relative_path=str(item.get("relative_path") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
                sha256=str(item.get("sha256") or ""),
                mode=int(item.get("mode") or 0),
            )
        )
    return tuple(files)


class CommandKernel:
    def __init__(self, store_root: Path, authority: CommandGrantAuthority) -> None:
        self.store = CommandJobStore(Path(store_root))
        self.authority = authority
        self.authority.bind_ledger(self.store.root / "grant-nonces.json")
        self._lock = threading.Lock()
        self._live: dict[str, SpawnedCommand] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._receipts: dict[str, CommandReceipt] = {}

    def submit(self, request: CommandRequest, grant_token: str, *, actor_id: str) -> str:
        existing = self.store.lookup_idempotency(actor_id, request.idempotency_key)
        if existing is not None:
            if existing["digest"] != request.digest:
                raise CommandError("idempotency_conflict")
            return existing["job_id"]
        grant = self.authority.verify(grant_token, request, actor_id=actor_id)
        argv, resolved = resolve_request(request, grant)
        self.authority.still_valid(grant)
        for item in argv:
            if item.startswith("/proc/") or item.startswith("/sys/") or item.startswith("/dev/"):
                raise CommandError("forbidden_path")
            if "docker.sock" in item:
                raise CommandError("forbidden_path")
        job_id = secrets.token_hex(16)
        job_dir = self.store.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(job_dir, 0o700)
        workspace = JobWorkspace(job_dir)
        workspace.materialize(stdin=request.stdin)
        state = {
            "actor_id": actor_id,
            "argv": list(argv),
            "command_digest": request.digest,
            "effect_boundary_crossed": False,
            "error_code": "",
            "executable": {
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
            "grant_nonce": grant.nonce,
            "idempotency_key": request.idempotency_key,
            "job_id": job_id,
            "lane": request.lane.value,
            "max_stderr_bytes": request.max_stderr_bytes,
            "max_stdout_bytes": request.max_stdout_bytes,
            "origin": request.origin.value,
            "pid": None,
            "status": CommandStatus.ADMITTED.value,
            "timeout_sec": request.timeout_sec,
            "turn_id": grant.turn_id,
        }
        self.store.write_state(job_id, state)
        atomic_write_json(job_dir / "request.json", {"argv": list(argv), "digest": request.digest, "lane": request.lane.value})
        self.store.remember_idempotency(actor_id, request.idempotency_key, job_id, request.digest)
        spawned = SpawnedCommand(
            argv=argv,
            workspace=workspace,
            timeout_sec=request.timeout_sec,
            max_stdout_bytes=request.max_stdout_bytes,
            max_stderr_bytes=request.max_stderr_bytes,
        )
        try:
            self.authority.still_valid(grant)
            spawned.spawn(resolved)
        except CommandError as exc:
            state["status"] = CommandStatus.FAILED.value
            state["error_code"] = exc.code
            state["finished_at"] = time.time()
            self.store.write_state(job_id, state)
            receipt = self._receipt_from_spawned(job_id, request, resolved, spawned, error_code=exc.code, status=CommandStatus.FAILED)
            self._receipts[job_id] = receipt
            raise
        state["status"] = CommandStatus.RUNNING.value
        state["pid"] = spawned.process.pid if spawned.process is not None else None
        state["started_at"] = spawned.started_at
        state["effect_boundary_crossed"] = True
        self.store.write_state(job_id, state)
        with self._lock:
            self._live[job_id] = spawned
        worker = threading.Thread(
            target=self._reap,
            args=(job_id, request, resolved, spawned),
            name=f"engineer-command-{job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads[job_id] = worker
        worker.start()
        return job_id

    def progress(self, job_id: str) -> CommandProgress:
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
            )
        state = self.store.read_state(job_id)
        status = CommandStatus(str(state.get("status") or CommandStatus.UNKNOWN.value))
        if status is CommandStatus.RUNNING:
            pid = int(state.get("pid") or 0)
            if not _pid_alive(pid):
                status = CommandStatus.UNKNOWN
                state["status"] = status.value
                state["error_code"] = "unknown_after_restart"
                self.store.write_state(job_id, state)
        started = float(state.get("started_at") or time.time())
        finished = state.get("finished_at")
        elapsed = float(finished or time.time()) - started
        stdout_bytes = 0
        stderr_bytes = 0
        workspace = JobWorkspace(self.store.job_dir(job_id))
        if workspace.stdout_path.exists():
            stdout_bytes = workspace.stdout_path.stat().st_size
        if workspace.stderr_path.exists():
            stderr_bytes = workspace.stderr_path.stat().st_size
        return CommandProgress(
            job_id=job_id,
            status=status,
            elapsed_sec=max(0.0, elapsed),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            output_activity=bool(stdout_bytes or stderr_bytes),
        )

    def cancel(self, job_id: str) -> None:
        with self._lock:
            live = self._live.get(job_id)
        if live is None:
            raise CommandError("job_not_running")
        live.request_cancel()

    def wait(self, job_id: str, *, timeout_sec: float | None = None) -> CommandReceipt:
        with self._lock:
            receipt = self._receipts.get(job_id)
            worker = self._threads.get(job_id)
        if receipt is not None:
            return receipt
        if worker is not None:
            worker.join(timeout=timeout_sec)
            with self._lock:
                receipt = self._receipts.get(job_id)
            if receipt is not None:
                return receipt
            raise CommandError("wait_timeout")
        return self._receipt_from_state(job_id)

    def _reap(
        self,
        job_id: str,
        request: CommandRequest,
        resolved: ResolvedExecutable,
        spawned: SpawnedCommand,
    ) -> None:
        error_code = ""
        status = CommandStatus.FAILED
        generated: tuple[GeneratedFile, ...] = ()
        try:
            spawned.wait()
            try:
                generated = spawned.workspace.admit_generated_files()
            except CommandError as exc:
                error_code = exc.code
                status = CommandStatus.FAILED
            else:
                if spawned.timed_out:
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
            error_code = exc.code
            status = CommandStatus.FAILED
        receipt = self._receipt_from_spawned(
            job_id,
            request,
            resolved,
            spawned,
            error_code=error_code,
            status=status,
            generated=generated,
        )
        state = self.store.read_state(job_id)
        state.update(
            {
                "cancelled": receipt.cancelled,
                "error_code": receipt.error_code,
                "exit_code": receipt.exit_code,
                "finished_at": receipt.finished_at,
                "generated_files": [
                    {
                        "mode": item.mode,
                        "relative_path": item.relative_path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in receipt.generated_files
                ],
                "signal": receipt.signal,
                "status": receipt.status.value,
                "stderr_sha256": receipt.stderr_sha256,
                "stdout_sha256": receipt.stdout_sha256,
                "timed_out": receipt.timed_out,
                "truncated_stderr": receipt.truncated_stderr,
                "truncated_stdout": receipt.truncated_stdout,
            }
        )
        self.store.write_state(job_id, state)
        atomic_write_json(
            self.store.job_dir(job_id) / "receipt.json",
            receipt.to_public_payload(),
        )
        with self._lock:
            self._receipts[job_id] = receipt
            self._live.pop(job_id, None)

    def _receipt_from_spawned(
        self,
        job_id: str,
        request: CommandRequest,
        resolved: ResolvedExecutable,
        spawned: SpawnedCommand,
        *,
        error_code: str,
        status: CommandStatus,
        generated: tuple[GeneratedFile, ...] = (),
    ) -> CommandReceipt:
        return CommandReceipt(
            job_id=job_id,
            status=status,
            lane=request.lane,
            origin=request.origin,
            argv=spawned.argv,
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
        )

    def _receipt_from_state(self, job_id: str) -> CommandReceipt:
        state = self.store.read_state(job_id)
        status = CommandStatus(str(state.get("status") or CommandStatus.UNKNOWN.value))
        if status is CommandStatus.RUNNING and not _pid_alive(int(state.get("pid") or 0)):
            status = CommandStatus.UNKNOWN
            state["status"] = status.value
            state["error_code"] = "unknown_after_restart"
            self.store.write_state(job_id, state)
        workspace = JobWorkspace(self.store.job_dir(job_id))
        stdout = workspace.stdout_path.read_bytes() if workspace.stdout_path.exists() else b""
        stderr = workspace.stderr_path.read_bytes() if workspace.stderr_path.exists() else b""
        lane = CommandLane(str(state.get("lane") or CommandLane.ARGV.value))
        origin = CommandOrigin(str(state.get("origin") or CommandOrigin.OWNER_TURN.value))
        argv = tuple(str(item) for item in (state.get("argv") or ()))
        return CommandReceipt(
            job_id=job_id,
            status=status,
            lane=lane,
            origin=origin,
            argv=argv,
            exit_code=state.get("exit_code"),
            signal=state.get("signal"),
            timed_out=bool(state.get("timed_out")),
            cancelled=bool(state.get("cancelled")),
            truncated_stdout=bool(state.get("truncated_stdout")),
            truncated_stderr=bool(state.get("truncated_stderr")),
            started_at=float(state.get("started_at") or 0.0),
            finished_at=state.get("finished_at"),
            executable=_resolved_from_state(state),
            stdout_sha256=str(state.get("stdout_sha256") or sha256_bytes(stdout)),
            stderr_sha256=str(state.get("stderr_sha256") or sha256_bytes(stderr)),
            stdout=stdout,
            stderr=stderr,
            generated_files=_files_from_state(state),
            error_code=str(state.get("error_code") or ""),
            effect_boundary_crossed=bool(state.get("effect_boundary_crossed")),
        )
