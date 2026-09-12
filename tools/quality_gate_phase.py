"""The existing Linux gate parent's command loop and recursive cleanup fence.

No controller is added: pytest remains one command per canonical phase. Signal
handlers record cancellation without interrupting Popen's ownership assignment;
the loop observes it within 50ms. Children inherit no blocked cancellation mask.
An uncertain cleanup permanently fences both later commands and scratch removal.
"""

from __future__ import annotations

import json
import os
import select
import signal
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .quality_gate_deadlines import (
    CaseDeadline,
    DeadlineEvent,
    DeadlinePlan,
    NodeDeadline,
    ParentDeadlineLedger,
    encode_event,
    seal_plan,
)
from .quality_gate_process import OwnedCommandScope

POLL_SECONDS = 0.05
FIFO_OPTION = "--friday-deadline-fifo"
RUN_OPTION = "--friday-deadline-run"
PLAN_OPTION = "--friday-deadline-plan"
PHASE_OPTION = "--friday-deadline-phase"


def gate_plan(selected: Any, matrix: Any, run_id: str) -> DeadlinePlan:
    """Seal unchanged inventory node ceilings and every fully selected case."""
    specs = tuple(
        NodeDeadline(node.nodeid, "UI" if node.execution_kind == "browser" else "non-UI", node.max_runtime_s)
        for node in selected
    )
    ids = {node.nodeid for node in specs}
    cases = []
    for case in matrix["cases"]:
        if case.get("executable") is not True:
            continue
        members = case["node_ids"]
        if ids.intersection(members):
            if not set(members).issubset(ids):
                raise ValueError("gate_deadline_case_partially_selected")
            cases.append(CaseDeadline(case["id"], tuple(members), case["timeout_s"]))
    phases = tuple(label for label in ("non-UI", "UI") if any(node.phase == label for node in specs))
    return seal_plan(run_id=run_id, phases=phases, nodes=specs, cases=tuple(cases))


def validate_deadline_evidence(value: Any, plan: DeadlinePlan) -> None:
    """Replay the recorded arrival order, including its original event digest.

    This validates a hash-bound canonical writer's receipt; replay is never
    substituted for actually running its parent loop and observing cleanup.
    """
    try:
        replay = ParentDeadlineLedger(plan)
        if not isinstance(value, dict) or not isinstance(value["attempts"], list):
            raise ValueError
        if len(value["attempts"]) != len(plan.nodes) or len(value["phases"]) != len(plan.phases):
            raise ValueError
        frames = []
        for row in value["attempts"]:
            for kind in ("start", "finish"):
                index = row[f"{kind}_event_index"]
                if type(index) is not int:
                    raise ValueError
                frames.append(
                    (
                        index,
                        DeadlineEvent(
                            plan.run_id,
                            plan.sha256,
                            row["phase"],
                            row["worker"],
                            row[f"{kind}_sequence"],
                            kind,
                            row["node_sha256"],
                            row[f"{kind}_ns"],
                        ),
                    )
                )
        frames.sort(key=lambda pair: pair[0])
        if [index for index, _ in frames] != list(range(1, 2 * len(plan.nodes) + 1)):
            raise ValueError
        at = 0
        for phase in value["phases"]:
            replay.begin_phase(phase["phase"], phase["launch_ns"])
            while at < len(frames) and frames[at][1].phase == phase["phase"]:
                replay.feed(encode_event(frames[at][1]), phase["finish_ns"])
                at += 1
            replay.close_phase(phase["phase"], phase["finish_ns"], succeeded=True)
        result = replay.complete(value["last_observed_ns"], succeeded=True)
        if at != len(frames) or json.dumps(result, sort_keys=True) != json.dumps(value, sort_keys=True):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("gate_deadline_receipt_invalid") from exc


def validate_process_evidence(value: Any, names: Any, deadline: Any) -> None:
    """Require successful exclusive cleanup for each completed canonical step."""

    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("gate_process_receipt_invalid")

    require(isinstance(value, list) and len(value) == len(names))
    previous = 0
    phases = {row["phase"]: row for row in deadline["phases"]}
    for row, name in zip(value, names, strict=True):
        require(
            isinstance(row, dict)
            and set(row) == {"name", "start_ns", "finish_ns", "phase", "cleanup", "status"}
        )
        require(row["name"] == name and row["status"] == "passed")
        require(
            type(row["start_ns"]) is int
            and type(row["finish_ns"]) is int
            and previous <= row["start_ns"] <= row["finish_ns"] < 2**63
        )
        previous = row["finish_ns"]
        label = next((label for label in phases if name == f"exact-release {label} tests"), None)
        require(row["phase"] == label)
        if label is not None:
            require(
                row["start_ns"] == phases[label]["launch_ns"]
                and row["finish_ns"] <= phases[label]["finish_ns"]
            )
        proof = row["cleanup"]
        require(
            isinstance(proof, dict)
            and set(proof)
            == {
                "schema",
                "leader_returncode",
                "leader_reaped",
                "reaped_descendants",
                "forced_leader",
                "forced_descendants",
                "kernel_echild",
                "subreaper_restored",
                "failure_codes",
            }
        )
        require(
            proof["schema"] == "friday.quality-gate-child-cleanup.v1"
            and type(proof["leader_returncode"]) is int
            and proof["leader_returncode"] == 0
            and type(proof["reaped_descendants"]) is int
            and proof["reaped_descendants"] >= 0
        )
        require(all(proof[key] is True for key in ("leader_reaped", "kernel_echild", "subreaper_restored")))
        require(
            proof["forced_leader"] is False
            and proof["forced_descendants"] is False
            and proof["failure_codes"] == []
        )


def validate_auxiliary_evidence(
    value: Any, measured: Any, deadline: Any, *, expected_clones: int = 1
) -> None:
    """Close early/late Git, clone and host reads under the same ownership proof."""
    try:
        if not isinstance(value, list) or not 0 < len(value) <= 10_000:
            raise ValueError
        names = [row["name"] for row in value]
        if (
            set(names) != {"candidate Git read", "private candidate clone", "exact-host prerequisite"}
            or names.count("private candidate clone") != expected_clones
        ):
            raise ValueError
        validate_process_evidence(value, names, {"phases": []})
        phase_end = {row["phase"]: row["finish_ns"] for row in deadline["phases"]}
        previous = 0
        for row in sorted((*value, *measured), key=lambda row: row["start_ns"]):
            if row["start_ns"] < previous:
                raise ValueError
            previous = max(row["finish_ns"], phase_end.get(row["phase"], 0))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("gate_auxiliary_receipt_invalid") from exc


@dataclass
class _Pipe:
    reader: int
    keeper: int | None

    def close_keeper(self) -> None:
        if self.keeper is not None:
            os.close(self.keeper)
            self.keeper = None


class GateProcessRunner:
    """Exclusive command owner; retain one instance for the entire tier."""

    def __init__(self) -> None:
        self.cleanup_safe = True
        self.cancelled_signal: int | None = None
        self.commands: list[dict[str, Any]] = []
        self._handlers: dict[signal.Signals, Any] = {}

    def _cancel(self, signum: int, _frame: Any) -> None:
        if self.cancelled_signal is None:
            self.cancelled_signal = signum

    def check(self) -> None:
        if not self.cleanup_safe:
            raise RuntimeError("gate_cleanup_uncertain_scratch_retained")
        if self.cancelled_signal is not None:
            raise RuntimeError(f"gate_cancelled_{self.cancelled_signal}")

    def __enter__(self) -> GateProcessRunner:
        if self._handlers:
            raise RuntimeError("gate_signal_scope_reentered")
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
        if {signal.SIGINT, signal.SIGTERM}.intersection(mask):
            raise RuntimeError("gate_cancellation_signals_blocked")
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self._handlers[signum] = signal.signal(signum, self._cancel)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        for signum, handler in self._handlers.items():
            signal.signal(signum, handler)
        self._handlers.clear()

    def capture(self, command: Any, *, limit: int) -> bytes:
        """Bounded output for the same parent's fixed Git/host admission reads."""
        if type(limit) is not int or not 0 < limit <= 16 << 20:
            raise ValueError("gate_capture_limit_invalid")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            self(command, _capture=(output, errors, limit))
            output.seek(0)
            raw = output.read(limit + 1)
            if len(raw) > limit:
                raise RuntimeError("gate_capture_limit_exceeded")
            return raw

    @contextmanager
    def _events(
        self, command: Any, ledger: ParentDeadlineLedger | None, phase: str | None, fifo: Path | None
    ) -> Iterator[tuple[Any, _Pipe | None]]:
        if ledger is None:
            if phase is not None or fifo is not None:
                raise ValueError("gate_event_plan_missing")
            yield command, None
            return
        if phase not in ledger.plan.phases or fifo is None or not fifo.is_absolute():
            raise ValueError("gate_event_phase_invalid")
        # The group root is already an exclusive 0700 gate scratch directory.
        parent = fifo.parent.lstat()
        if (
            fifo.parent.resolve(strict=True) != fifo.parent
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError("gate_event_directory_unsafe")
        os.mkfifo(fifo, 0o600)
        reader = keeper = None
        pipe = None
        try:
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
            keeper = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
            configured = replace(
                command,
                argv=(
                    *command.argv,
                    "-p",
                    "tools.quality_gate_deadline_plugin",
                    f"{FIFO_OPTION}={fifo}",
                    f"{RUN_OPTION}={ledger.plan.run_id}",
                    f"{PLAN_OPTION}={ledger.plan.sha256}",
                    f"{PHASE_OPTION}={phase}",
                    "--max-worker-restart=0",
                ),
            )
            pipe = _Pipe(reader, keeper)
            yield configured, pipe
        finally:
            if pipe is not None:
                pipe.close_keeper()
            elif keeper is not None:
                os.close(keeper)
            if reader is not None:
                os.close(reader)
            if self.cleanup_safe:
                fifo.unlink()

    @staticmethod
    def _drain(reader: int, ledger: ParentDeadlineLedger) -> bool:
        # Each successful frame consumes a previously unseen node transition.
        # Invalid, duplicate, oversized or overflowing input fails in the ledger.
        while True:
            try:
                chunk = os.read(reader, 65536)
            except BlockingIOError:
                return False
            if not chunk:
                return True
            ledger.feed(chunk, time.monotonic_ns())  # sample AFTER the read

    def __call__(
        self,
        command: Any,
        *,
        ledger: ParentDeadlineLedger | None = None,
        phase: str | None = None,
        fifo: Path | None = None,
        _capture: tuple[Any, Any, int] | None = None,
    ) -> int:
        self.check()
        if not self._handlers:
            raise RuntimeError("gate_signal_scope_missing")
        print(f"\n[{command.name}]", flush=True)
        with self._events(command, ledger, phase, fifo) as (configured, pipe):
            reader = pipe.reader if pipe is not None else None
            scope = OwnedCommandScope()
            started = time.monotonic_ns()
            row: dict[str, Any] = {"name": command.name, "start_ns": started, "phase": phase}
            self.commands.append(row)
            if ledger is not None:
                assert phase is not None
                ledger.begin_phase(phase, started)
            failure: BaseException | None = None
            # Set this before entering the scope: a pre-existing child is also
            # an uncertain ownership boundary, not permission to delete scratch.
            self.cleanup_safe = False
            try:
                scope.__enter__()
                scope.start(
                    configured.argv,
                    cwd=configured.cwd,
                    environment=configured.environment if configured.environment is not None else os.environ,
                    child_umask=configured.child_umask,
                    stdout=_capture[0] if _capture is not None else None,
                    stderr=_capture[1] if _capture is not None else None,
                )
                command_deadline = started + configured.timeout_s * 1_000_000_000
                while True:
                    if self.cancelled_signal is not None:
                        raise RuntimeError(f"gate_cancelled_{self.cancelled_signal}")
                    returncode = scope.poll()  # reap adopted zombies mid-phase
                    if (
                        _capture is not None
                        and sum(os.fstat(handle.fileno()).st_size for handle in _capture[:2]) > _capture[2]
                    ):
                        raise RuntimeError("gate_capture_limit_exceeded")
                    if reader is not None and ledger is not None:
                        self._drain(reader, ledger)
                    now = time.monotonic_ns()
                    if now > command_deadline:
                        raise RuntimeError("gate_controller_deadline_exceeded")
                    next_due = ledger.next_deadline_ns(now) if ledger is not None else None
                    if returncode is not None:
                        break
                    until = min(command_deadline + 1, next_due or command_deadline + 1)
                    timeout = min(POLL_SECONDS, max(0.0, (until - now) / 1_000_000_000))
                    select.select(() if reader is None else (reader,), (), (), timeout)
            except BaseException as exc:
                failure = exc
                if ledger is not None:
                    ledger.abort("gate_command_failed")
            finally:
                if scope.entered:
                    try:
                        proof = scope.close()
                        self.cleanup_safe = proof.cleanup_clear
                        row["cleanup"] = asdict(proof)
                        row["cleanup"]["failure_codes"] = list(proof.failure_codes)
                        if failure is None and not proof.clean_exit:
                            failure = RuntimeError("gate_command_not_clean")
                    except BaseException as exc:
                        failure = failure or exc
                row["finish_ns"] = time.monotonic_ns()
            if self.cancelled_signal is not None and failure is None:
                failure = RuntimeError(f"gate_cancelled_{self.cancelled_signal}")
            if failure is not None:
                row["status"] = "failed"
                if ledger is not None:
                    ledger.abort("gate_command_failed")
                raise failure
            if reader is not None and ledger is not None:
                assert pipe is not None
                pipe.close_keeper()
                try:
                    if not self._drain(reader, ledger):
                        self.cleanup_safe = False
                        raise RuntimeError("gate_event_writer_outside_owned_scope")
                    self.check()
                    assert phase is not None
                    ledger.close_phase(phase, time.monotonic_ns(), succeeded=True)
                except BaseException:
                    row["status"] = "failed"
                    ledger.abort("gate_event_close_failed")
                    raise
            row["status"] = "passed"
            return 0
