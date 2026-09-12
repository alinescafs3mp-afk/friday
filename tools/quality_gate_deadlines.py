"""Active deadlines for the canonical pytest gate.

The integration API is deliberately small:

* :func:`seal_plan` freezes the exact ordered nodes, phases, case memberships
  and unchanged second-based limits.  ``DeadlinePlan.sha256`` binds its strict
  ``evidence()`` fields; :func:`validate_plan` reconstructs that seal.
* :class:`EventWriter` lets an execution-side pytest hook write one bounded
  START or FINISH record.  :class:`EventDecoder` handles arbitrary parent read
  boundaries without retaining an oversized partial frame.
* :class:`ParentDeadlineLedger` is the single parent-owned state machine.  Its
  phase methods retain finished work across phases, ``next_deadline_ns`` returns
  the first nanosecond forbidden by the existing ``duration > limit`` rule,
  and ``complete`` can produce creditable evidence only after external pytest
  and JUnit success.

Evidence has the exact top-level fields emitted by ``evidence``: schema,
status, credit_eligible, failure, run/plan/event identities and counts, derived
retry_count, ordered phase rows, ordered node attempts, and ordered case totals.
The module starts no process, thread, controller, alarm, or timer.  The caller
must continuously drain ready records before checking a deadline, then provide
recursive process cleanup and keep JUnit as an independent bound.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NoReturn

__all__ = (
    "CaseDeadline",
    "DeadlineError",
    "DeadlineEvent",
    "DeadlinePlan",
    "EVIDENCE_SCHEMA",
    "EventDecoder",
    "EventWriter",
    "NodeDeadline",
    "PIPE_ATOMIC_BYTES",
    "PLAN_SCHEMA",
    "ParentDeadlineLedger",
    "decode_event",
    "encode_event",
    "nodeid_sha256",
    "seal_plan",
    "validate_plan",
    "write_event",
)

EVENT_VERSION = 1
PLAN_SCHEMA = "friday.quality-gate-deadline-plan.v1"
EVIDENCE_SCHEMA = "friday.quality-gate-deadline-evidence.v2"
PIPE_ATOMIC_BYTES = 512  # POSIX minimum PIPE_BUF, including the final LF.
MAX_FEED_BYTES = 64 << 10
MAX_NODEID_BYTES = 256 << 10
MAX_PLAN_BYTES = 64 << 20
MAX_NODES = 100_000
MAX_CASES = 100_000
MAX_MEMBERSHIPS = 1_000_000
MAX_SECONDS = 86_400
MAX_TIME_NS = (1 << 63) - 1
MAX_SEQUENCE = (1 << 31) - 1
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}").fullmatch
_DIGEST = re.compile(r"[0-9a-f]{64}").fullmatch
_EVENT_KINDS = frozenset({"start", "finish"})
_EVENT_KEYS = frozenset({"v", "run", "plan", "phase", "worker", "seq", "event", "node", "t_ns"})
_NODE_DIGEST_DOMAIN = b"friday-quality-gate-node-v1\0"
_PLAN_DIGEST_DOMAIN = b"friday-quality-gate-deadline-plan-v1\0"


class DeadlineError(ValueError):
    """A closed-plan, codec, event-stream or deadline violation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _error(code: str) -> NoReturn:
    raise DeadlineError(code)


def _token(value: object, code: str) -> str:
    if not isinstance(value, str) or _TOKEN(value) is None:
        _error(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _DIGEST(value) is None:
        _error(code)
    return value


def _integer(value: object, minimum: int, maximum: int, code: str) -> int:
    # bool is an int subclass and must never satisfy a time/count field.
    if type(value) is not int or not minimum <= value <= maximum:
        _error(code)
    return value


def _nodeid(value: object) -> str:
    if not isinstance(value, str) or not value:
        _error("plan_nodeid_invalid")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        _error("plan_nodeid_invalid")
    if len(raw) > MAX_NODEID_BYTES or any(unicodedata.category(character) == "Cc" for character in value):
        _error("plan_nodeid_invalid")
    return value


def nodeid_sha256(nodeid: str) -> str:
    """Return the event identity for one already collected exact node ID."""

    value = _nodeid(nodeid)
    raw = value.encode("utf-8")
    digest = hashlib.sha256(_NODE_DIGEST_DOMAIN)
    digest.update(len(raw).to_bytes(4, "big"))
    digest.update(raw)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class NodeDeadline:
    """One unique selected node and its unchanged inventory ceiling."""

    nodeid: str
    phase: str
    limit_s: int


@dataclass(frozen=True, slots=True)
class CaseDeadline:
    """One declared case, its ordered unique members and matrix ceiling."""

    case_id: str
    members: tuple[str, ...]
    limit_s: int


@dataclass(frozen=True, slots=True)
class _SealedNode:
    nodeid: str
    node_sha256: str
    phase: str
    limit_ns: int


@dataclass(frozen=True, slots=True)
class _SealedCase:
    case_id: str
    members: tuple[str, ...]
    member_sha256s: tuple[str, ...]
    limit_ns: int


@dataclass(frozen=True, slots=True)
class DeadlinePlan:
    """Immutable exact collection/case plan consumed by the parent ledger."""

    run_id: str
    phases: tuple[str, ...]
    nodes: tuple[_SealedNode, ...]
    cases: tuple[_SealedCase, ...]
    sha256: str
    node_by_digest: Mapping[str, _SealedNode] = field(repr=False, compare=False)
    node_by_id: Mapping[str, _SealedNode] = field(repr=False, compare=False)
    cases_by_node: Mapping[str, tuple[_SealedCase, ...]] = field(repr=False, compare=False)

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "run_id": self.run_id,
            "phases": list(self.phases),
            "nodes": [
                {
                    "nodeid": node.nodeid,
                    "node_sha256": node.node_sha256,
                    "phase": node.phase,
                    "limit_ns": node.limit_ns,
                }
                for node in self.nodes
            ],
            "cases": [
                {
                    "case_id": case.case_id,
                    "members": list(case.members),
                    "member_sha256s": list(case.member_sha256s),
                    "limit_ns": case.limit_ns,
                }
                for case in self.cases
            ],
        }


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_plan(
    *,
    run_id: str,
    phases: tuple[str, ...],
    nodes: tuple[NodeDeadline, ...],
    cases: tuple[CaseDeadline, ...],
) -> DeadlinePlan:
    """Validate and seal one exact tier plan before the first pytest phase."""

    run = _token(run_id, "plan_run_id_invalid")
    if type(phases) is not tuple or not phases or len(phases) > 64:
        _error("plan_phases_invalid")
    exact_phases = tuple(_token(value, "plan_phase_invalid") for value in phases)
    if len(exact_phases) != len(set(exact_phases)):
        _error("plan_phases_duplicate")
    if type(nodes) is not tuple or not 0 < len(nodes) <= MAX_NODES:
        _error("plan_nodes_invalid")
    if type(cases) is not tuple or not 0 < len(cases) <= MAX_CASES:
        _error("plan_cases_invalid")

    sealed_nodes: list[_SealedNode] = []
    node_ids: set[str] = set()
    node_digests: dict[str, str] = {}
    used_phases: set[str] = set()
    encoded_plan_text_bytes = 0
    for node_spec in nodes:
        if type(node_spec) is not NodeDeadline:
            _error("plan_node_invalid")
        nodeid = _nodeid(node_spec.nodeid)
        phase = _token(node_spec.phase, "plan_node_phase_invalid")
        limit_s = _integer(node_spec.limit_s, 1, MAX_SECONDS, "plan_node_limit_invalid")
        if nodeid in node_ids:
            _error("plan_node_duplicate")
        if phase not in exact_phases:
            _error("plan_node_phase_unknown")
        encoded_plan_text_bytes += len(_canonical_json(nodeid))
        if encoded_plan_text_bytes > MAX_PLAN_BYTES:
            _error("plan_oversized")
        identity = nodeid_sha256(nodeid)
        _digest(identity, "plan_node_digest_invalid")
        collision = node_digests.get(identity)
        if collision is not None and collision != nodeid:
            _error("plan_node_digest_collision")
        node_ids.add(nodeid)
        node_digests[identity] = nodeid
        used_phases.add(phase)
        sealed_nodes.append(_SealedNode(nodeid, identity, phase, limit_s * 1_000_000_000))
    if used_phases != set(exact_phases):
        _error("plan_phase_membership_inexact")

    sealed_by_id = {node.nodeid: node for node in sealed_nodes}
    sealed_cases: list[_SealedCase] = []
    case_ids: set[str] = set()
    memberships = 0
    for case_spec in cases:
        if type(case_spec) is not CaseDeadline:
            _error("plan_case_invalid")
        case_id = _token(case_spec.case_id, "plan_case_id_invalid")
        limit_s = _integer(case_spec.limit_s, 1, MAX_SECONDS, "plan_case_limit_invalid")
        if case_id in case_ids:
            _error("plan_case_duplicate")
        if type(case_spec.members) is not tuple or not case_spec.members:
            _error("plan_case_members_invalid")
        if memberships + len(case_spec.members) > MAX_MEMBERSHIPS:
            _error("plan_memberships_oversized")
        encoded_plan_text_bytes += len(_canonical_json(case_id))
        members_list: list[str] = []
        member_set: set[str] = set()
        for raw_member in case_spec.members:
            member = _nodeid(raw_member)
            if member in member_set:
                _error("plan_case_member_duplicate")
            if member not in sealed_by_id:
                _error("plan_case_member_unknown")
            encoded_plan_text_bytes += len(_canonical_json(member))
            if encoded_plan_text_bytes > MAX_PLAN_BYTES:
                _error("plan_oversized")
            member_set.add(member)
            members_list.append(member)
        members = tuple(members_list)
        memberships += len(members)
        case_ids.add(case_id)
        sealed_cases.append(
            _SealedCase(
                case_id,
                members,
                tuple(sealed_by_id[member].node_sha256 for member in members),
                limit_s * 1_000_000_000,
            )
        )

    provisional = DeadlinePlan(
        run,
        exact_phases,
        tuple(sealed_nodes),
        tuple(sealed_cases),
        "0" * 64,
        MappingProxyType({node.node_sha256: node for node in sealed_nodes}),
        MappingProxyType(sealed_by_id),
        MappingProxyType({}),
    )
    raw = _canonical_json(provisional.evidence())
    if not raw or len(raw) > MAX_PLAN_BYTES:
        _error("plan_oversized")
    digest = hashlib.sha256(_PLAN_DIGEST_DOMAIN + raw).hexdigest()
    by_node: dict[str, list[_SealedCase]] = {node.node_sha256: [] for node in sealed_nodes}
    for case in sealed_cases:
        for identity in case.member_sha256s:
            by_node[identity].append(case)
    return DeadlinePlan(
        run,
        exact_phases,
        tuple(sealed_nodes),
        tuple(sealed_cases),
        digest,
        MappingProxyType({node.node_sha256: node for node in sealed_nodes}),
        MappingProxyType(sealed_by_id),
        MappingProxyType({key: tuple(value) for key, value in by_node.items()}),
    )


def validate_plan(value: object) -> DeadlinePlan:
    """Rebuild a supplied plan so direct dataclass construction cannot bypass sealing."""

    if type(value) is not DeadlinePlan:
        _error("ledger_plan_invalid")
    assert isinstance(value, DeadlinePlan)
    if (
        type(value.phases) is not tuple
        or type(value.nodes) is not tuple
        or type(value.cases) is not tuple
        or not all(type(node) is _SealedNode for node in value.nodes)
        or not all(type(case) is _SealedCase for case in value.cases)
    ):
        _error("ledger_plan_invalid")
    try:
        nodes = tuple(
            NodeDeadline(
                node.nodeid,
                node.phase,
                _integer(node.limit_ns, 1, MAX_SECONDS * 1_000_000_000, "ledger_plan_invalid")
                // 1_000_000_000,
            )
            for node in value.nodes
        )
        cases = tuple(
            CaseDeadline(
                case.case_id,
                case.members,
                _integer(case.limit_ns, 1, MAX_SECONDS * 1_000_000_000, "ledger_plan_invalid")
                // 1_000_000_000,
            )
            for case in value.cases
        )
    except (AttributeError, TypeError) as exc:
        raise DeadlineError("ledger_plan_invalid") from exc
    if any(node.limit_ns % 1_000_000_000 for node in value.nodes) or any(
        case.limit_ns % 1_000_000_000 for case in value.cases
    ):
        _error("ledger_plan_invalid")
    try:
        rebuilt = seal_plan(run_id=value.run_id, phases=value.phases, nodes=nodes, cases=cases)
    except DeadlineError as exc:
        raise DeadlineError("ledger_plan_invalid") from exc
    if (
        rebuilt.sha256 != value.sha256
        or rebuilt.nodes != value.nodes
        or rebuilt.cases != value.cases
        or rebuilt.run_id != value.run_id
        or rebuilt.phases != value.phases
    ):
        _error("ledger_plan_invalid")
    return rebuilt


@dataclass(frozen=True, slots=True)
class DeadlineEvent:
    run_id: str
    plan_sha256: str
    phase: str
    worker: str
    sequence: int
    event: str
    node_sha256: str
    timestamp_ns: int


def _validate_event(event: object) -> DeadlineEvent:
    if type(event) is not DeadlineEvent:
        _error("event_shape_invalid")
    assert isinstance(event, DeadlineEvent)
    _token(event.run_id, "event_run_invalid")
    _digest(event.plan_sha256, "event_plan_invalid")
    _token(event.phase, "event_phase_invalid")
    _token(event.worker, "event_worker_invalid")
    _integer(event.sequence, 1, MAX_SEQUENCE, "event_sequence_invalid")
    if not isinstance(event.event, str) or event.event not in _EVENT_KINDS:
        _error("event_kind_invalid")
    _digest(event.node_sha256, "event_node_invalid")
    _integer(event.timestamp_ns, 0, MAX_TIME_NS, "event_timestamp_invalid")
    return event


def _event_object(event: DeadlineEvent) -> dict[str, object]:
    return {
        "v": EVENT_VERSION,
        "run": event.run_id,
        "plan": event.plan_sha256,
        "phase": event.phase,
        "worker": event.worker,
        "seq": event.sequence,
        "event": event.event,
        "node": event.node_sha256,
        "t_ns": event.timestamp_ns,
    }


def encode_event(event: DeadlineEvent) -> bytes:
    """Encode one canonical LF record guaranteed within POSIX PIPE_BUF."""

    value = _validate_event(event)
    raw = _canonical_json(_event_object(value)) + b"\n"
    if len(raw) > PIPE_ATOMIC_BYTES:
        _error("event_oversized")
    return raw


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _error("event_duplicate_key")
        value[key] = item
    return value


def decode_event(frame: bytes) -> DeadlineEvent:
    """Decode exactly one bounded canonical record, including its final LF."""

    if type(frame) is not bytes or not frame or len(frame) > PIPE_ATOMIC_BYTES:
        _error("event_frame_oversized_or_empty")
    if not frame.endswith(b"\n") or frame.count(b"\n") != 1 or b"\r" in frame or b"\0" in frame:
        _error("event_frame_invalid")
    try:
        value = json.loads(frame[:-1], object_pairs_hook=_strict_object)
    except DeadlineError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeadlineError("event_json_invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _EVENT_KEYS
        or type(value.get("v")) is not int
        or value["v"] != 1
    ):
        _error("event_shape_invalid")
    event = DeadlineEvent(
        value["run"],
        value["plan"],
        value["phase"],
        value["worker"],
        value["seq"],
        value["event"],
        value["node"],
        value["t_ns"],
    )
    _validate_event(event)
    if encode_event(event) != frame:
        _error("event_not_canonical")
    return event


class EventDecoder:
    """Incremental LF decoder with a one-record bounded retained tail."""

    __slots__ = ("_tail", "_closed")

    def __init__(self) -> None:
        self._tail = bytearray()
        self._closed = False

    def feed(self, chunk: bytes) -> tuple[DeadlineEvent, ...]:
        if self._closed:
            _error("event_stream_closed")
        if type(chunk) is not bytes or len(chunk) > MAX_FEED_BYTES:
            _error("event_chunk_invalid")
        events: list[DeadlineEvent] = []
        offset = 0
        while True:
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                tail = chunk[offset:]
                if len(self._tail) + len(tail) >= PIPE_ATOMIC_BYTES:
                    _error("event_frame_oversized")
                self._tail.extend(tail)
                return tuple(events)
            part = chunk[offset : newline + 1]
            if len(self._tail) + len(part) > PIPE_ATOMIC_BYTES:
                _error("event_frame_oversized")
            frame = bytes(self._tail) + part
            self._tail.clear()
            events.append(decode_event(frame))
            offset = newline + 1

    def finish(self) -> None:
        if self._closed:
            _error("event_stream_closed")
        self._closed = True
        if self._tail:
            _error("event_frame_missing_terminator")


def write_event(descriptor: int, event: DeadlineEvent) -> int:
    """Write exactly one atomic event to an already opened pipe/FIFO."""

    fd = _integer(descriptor, 0, (1 << 31) - 1, "event_descriptor_invalid")
    try:
        mode = os.fstat(fd).st_mode
        pipe_buf = os.fpathconf(fd, "PC_PIPE_BUF")
    except OSError as exc:
        raise DeadlineError("event_descriptor_invalid") from exc
    if not stat.S_ISFIFO(mode) or type(pipe_buf) is not int or pipe_buf < 1:
        _error("event_descriptor_not_pipe")
    raw = encode_event(event)
    if len(raw) > pipe_buf:
        _error("event_exceeds_pipe_buf")
    try:
        written = os.write(fd, raw)
    except OSError as exc:
        raise DeadlineError("event_write_failed") from exc
    if written != len(raw):
        _error("event_short_write")
    return written


class EventWriter:
    """Worker-local, threadless hook adapter; create one per execution phase."""

    __slots__ = ("descriptor", "run_id", "plan_sha256", "phase", "worker", "sequence")

    def __init__(self, descriptor: int, *, run_id: str, plan_sha256: str, phase: str, worker: str) -> None:
        self.descriptor = _integer(descriptor, 0, (1 << 31) - 1, "event_descriptor_invalid")
        self.run_id = _token(run_id, "event_run_invalid")
        self.plan_sha256 = _digest(plan_sha256, "event_plan_invalid")
        self.phase = _token(phase, "event_phase_invalid")
        self.worker = _token(worker, "event_worker_invalid")
        self.sequence = 0

    def emit(self, kind: str, nodeid: str, timestamp_ns: int | None = None) -> DeadlineEvent:
        next_sequence = self.sequence + 1
        _integer(next_sequence, 1, MAX_SEQUENCE, "event_sequence_invalid")
        at = time.monotonic_ns() if timestamp_ns is None else timestamp_ns
        event = DeadlineEvent(
            self.run_id,
            self.plan_sha256,
            self.phase,
            self.worker,
            next_sequence,
            kind,
            nodeid_sha256(nodeid),
            at,
        )
        write_event(self.descriptor, event)
        self.sequence = next_sequence
        return event

    def start(self, nodeid: str, timestamp_ns: int | None = None) -> DeadlineEvent:
        return self.emit("start", nodeid, timestamp_ns)

    def finish(self, nodeid: str, timestamp_ns: int | None = None) -> DeadlineEvent:
        return self.emit("finish", nodeid, timestamp_ns)


@dataclass(slots=True)
class _Attempt:
    start_events: int = 0
    start_event_index: int | None = None
    finish_event_index: int | None = None
    worker: str | None = None
    start_sequence: int | None = None
    finish_sequence: int | None = None
    start_ns: int | None = None
    finish_ns: int | None = None


@dataclass(slots=True)
class _CaseState:
    finished_ns: int = 0
    active: set[str] = field(default_factory=set)


class ParentDeadlineLedger:
    """Strict first-attempt ledger retained across all sequential phases."""

    def __init__(self, plan: DeadlinePlan) -> None:
        self.plan = validate_plan(plan)
        self._attempts = {node.node_sha256: _Attempt() for node in self.plan.nodes}
        self._cases = {case.case_id: _CaseState() for case in self.plan.cases}
        self._workers: dict[tuple[str, str], tuple[int, int]] = {}
        self._worker_active: dict[tuple[str, str], str] = {}
        self._phase_index = 0
        self._active_phase: str | None = None
        self._phase_launch_ns: int | None = None
        self._phase_decoder: EventDecoder | None = None
        self._phase_rows: list[dict[str, int | str]] = []
        self._last_observed_ns: int | None = None
        self._failure: str | None = None
        self._completed = False
        self._event_count = 0
        self._event_digest = hashlib.sha256(b"friday-quality-gate-deadline-events-v1\0")

    def _raise_failed(self) -> NoReturn:
        _error(f"ledger_failed:{self._failure}")

    def _healthy(self) -> None:
        if self._failure is not None:
            self._raise_failed()
        if self._completed:
            _error("ledger_already_completed")

    def _fail(self, code: str) -> NoReturn:
        if self._failure is None:
            self._failure = code
        self._raise_failed()

    def abort(self, code: str) -> None:
        """Make an external command/JUnit/cleanup failure permanently non-crediting."""

        if self._completed:
            _error("ledger_already_completed")
        reason = _token(code, "ledger_abort_code_invalid")
        if self._failure is None:
            self._failure = reason

    def _observe(self, now_ns: int) -> int:
        try:
            now = _integer(now_ns, 0, MAX_TIME_NS, "ledger_time_invalid")
        except DeadlineError as exc:
            self._fail(exc.code)
        if self._last_observed_ns is not None and now < self._last_observed_ns:
            self._fail("ledger_parent_time_regressed")
        self._last_observed_ns = now
        return now

    def begin_phase(self, phase: str, launch_ns: int) -> None:
        self._healthy()
        expected = self.plan.phases[self._phase_index] if self._phase_index < len(self.plan.phases) else None
        if self._active_phase is not None or phase != expected:
            self._fail("ledger_phase_out_of_order")
        now = self._observe(launch_ns)
        self._active_phase = phase
        self._phase_launch_ns = now
        self._phase_decoder = EventDecoder()
        self._phase_rows.append({"phase": phase, "launch_ns": now})

    def feed(self, chunk: bytes, observed_ns: int) -> int:
        """Consume one read; caller drains all readable chunks before a deadline check."""

        self._healthy()
        if self._active_phase is None or self._phase_decoder is None or self._phase_launch_ns is None:
            self._fail("ledger_phase_not_active")
        now = self._observe(observed_ns)
        try:
            events = self._phase_decoder.feed(chunk)
        except DeadlineError as exc:
            self._fail(exc.code)
        for event in events:
            canonical = encode_event(event)
            self._event_digest.update(len(canonical).to_bytes(2, "big"))
            self._event_digest.update(canonical)
            self._event_count += 1
            self._ingest(event, now)
        return len(events)

    def _ingest(self, event: DeadlineEvent, observed_ns: int) -> None:
        assert self._active_phase is not None and self._phase_launch_ns is not None
        if event.run_id != self.plan.run_id:
            self._fail("ledger_wrong_run")
        if event.plan_sha256 != self.plan.sha256:
            self._fail("ledger_wrong_plan")
        if event.phase != self._active_phase:
            self._fail("ledger_wrong_phase")
        node = self.plan.node_by_digest.get(event.node_sha256)
        if node is None:
            self._fail("ledger_unknown_node")
        if node.phase != self._active_phase:
            self._fail("ledger_node_wrong_phase")
        if event.timestamp_ns > observed_ns:
            self._fail("ledger_event_from_future")
        if event.timestamp_ns < self._phase_launch_ns:
            self._fail("ledger_event_before_phase_launch")

        attempt = self._attempts[event.node_sha256]
        if event.event == "start":
            attempt.start_events += 1
        worker_key = (event.phase, event.worker)
        prior_worker = self._workers.get(worker_key)
        if prior_worker is None:
            if event.sequence != 1:
                self._fail("ledger_worker_sequence_missing")
        else:
            prior_sequence, prior_time = prior_worker
            if event.sequence != prior_sequence + 1:
                self._fail("ledger_worker_sequence_out_of_order")
            if event.timestamp_ns < prior_time:
                self._fail("ledger_worker_time_regressed")
        self._workers[worker_key] = (event.sequence, event.timestamp_ns)

        if event.event == "start":
            if attempt.start_ns is not None or attempt.finish_ns is not None:
                self._fail("ledger_duplicate_start")
            if worker_key in self._worker_active:
                self._fail("ledger_worker_already_active")
            attempt.worker = event.worker
            attempt.start_sequence = event.sequence
            attempt.start_event_index = self._event_count
            attempt.start_ns = event.timestamp_ns
            self._worker_active[worker_key] = event.node_sha256
            for case in self.plan.cases_by_node[event.node_sha256]:
                self._cases[case.case_id].active.add(event.node_sha256)
            return

        if attempt.start_ns is None:
            self._fail("ledger_finish_before_start")
        if attempt.finish_ns is not None:
            self._fail("ledger_duplicate_finish")
        if attempt.worker != event.worker:
            self._fail("ledger_finish_worker_mismatch")
        if self._worker_active.get(worker_key) != event.node_sha256:
            self._fail("ledger_worker_attempt_mismatch")
        if event.timestamp_ns < attempt.start_ns:
            self._fail("ledger_node_time_regressed")
        duration = event.timestamp_ns - attempt.start_ns
        attempt.finish_sequence = event.sequence
        attempt.finish_event_index = self._event_count
        attempt.finish_ns = event.timestamp_ns
        del self._worker_active[worker_key]
        exceeded_case = False
        for case in self.plan.cases_by_node[event.node_sha256]:
            state = self._cases[case.case_id]
            state.active.discard(event.node_sha256)
            state.finished_ns += duration
            exceeded_case = exceeded_case or state.finished_ns > case.limit_ns
        if duration > node.limit_ns:
            self._fail("ledger_node_deadline_exceeded")
        if exceeded_case:
            self._fail("ledger_case_deadline_exceeded")

    def _case_used(self, case: _SealedCase, now_ns: int) -> int:
        state = self._cases[case.case_id]
        active_ns = 0
        for identity in state.active:
            start = self._attempts[identity].start_ns
            assert start is not None
            active_ns += now_ns - start
        return state.finished_ns + active_ns

    def next_deadline_ns(self, now_ns: int) -> int | None:
        """Return the first integer nanosecond that would violate ``> limit``.

        Equality is valid.  Parallel members therefore advance a case at rate N,
        and the ``+1`` is required to avoid converting ``>`` into ``>=``.
        """

        self._healthy()
        now = self._observe(now_ns)
        boundaries: list[int] = []
        for node in self.plan.nodes:
            attempt = self._attempts[node.node_sha256]
            if attempt.start_ns is None or attempt.finish_ns is not None:
                continue
            used = now - attempt.start_ns
            if used > node.limit_ns:
                self._fail("ledger_node_deadline_exceeded")
            boundaries.append(now + (node.limit_ns - used) + 1)
        for case in self.plan.cases:
            state = self._cases[case.case_id]
            count = len(state.active)
            if not count:
                if state.finished_ns > case.limit_ns:
                    self._fail("ledger_case_deadline_exceeded")
                continue
            used = self._case_used(case, now)
            if used > case.limit_ns:
                self._fail("ledger_case_deadline_exceeded")
            boundaries.append(now + (case.limit_ns - used) // count + 1)
        return min(boundaries) if boundaries else None

    def close_phase(self, phase: str, finished_ns: int, *, succeeded: bool) -> None:
        """Close one phase after its process exited and its event FIFO reached EOF."""

        self._healthy()
        if type(succeeded) is not bool:
            self._fail("ledger_phase_result_invalid")
        if phase != self._active_phase or self._phase_decoder is None:
            self._fail("ledger_phase_close_mismatch")
        now = self._observe(finished_ns)
        try:
            self._phase_decoder.finish()
        except DeadlineError as exc:
            self._fail(exc.code)
        if not succeeded:
            self._fail("ledger_phase_failed")
        expected = tuple(node for node in self.plan.nodes if node.phase == phase)
        if any(
            self._attempts[node.node_sha256].start_events != 1
            or self._attempts[node.node_sha256].start_ns is None
            or self._attempts[node.node_sha256].finish_ns is None
            for node in expected
        ):
            self._fail("ledger_phase_events_missing")
        if any(worker_phase == phase for worker_phase, _worker in self._worker_active):
            self._fail("ledger_phase_worker_active")
        self.next_deadline_ns(now)
        self._phase_rows[-1]["finish_ns"] = now
        self._active_phase = None
        self._phase_launch_ns = None
        self._phase_decoder = None
        self._phase_index += 1

    def complete(self, finished_ns: int, *, succeeded: bool) -> dict[str, Any]:
        """Return certifying evidence only after external pytest/JUnit success."""

        self._healthy()
        if type(succeeded) is not bool:
            self._fail("ledger_run_result_invalid")
        self._observe(finished_ns)
        if not succeeded:
            self._fail("ledger_run_failed")
        if self._active_phase is not None or self._phase_index != len(self.plan.phases):
            self._fail("ledger_phases_incomplete")
        if any(
            attempt.start_events != 1 or attempt.start_ns is None or attempt.finish_ns is None
            for attempt in self._attempts.values()
        ):
            self._fail("ledger_membership_inexact")
        if self._event_count != 2 * len(self.plan.nodes):
            self._fail("ledger_event_count_inexact")
        self._completed = True
        return self.evidence()

    def evidence(self) -> dict[str, Any]:
        now = self._last_observed_ns or 0
        attempts = []
        for node in self.plan.nodes:
            attempt = self._attempts[node.node_sha256]
            duration = (
                attempt.finish_ns - attempt.start_ns
                if attempt.start_ns is not None and attempt.finish_ns is not None
                else None
            )
            attempts.append(
                {
                    "nodeid": node.nodeid,
                    "node_sha256": node.node_sha256,
                    "phase": node.phase,
                    "limit_ns": node.limit_ns,
                    "start_events": attempt.start_events,
                    "start_event_index": attempt.start_event_index,
                    "finish_event_index": attempt.finish_event_index,
                    "worker": attempt.worker,
                    "start_sequence": attempt.start_sequence,
                    "finish_sequence": attempt.finish_sequence,
                    "start_ns": attempt.start_ns,
                    "finish_ns": attempt.finish_ns,
                    "duration_ns": duration,
                }
            )
        cases = []
        for case in self.plan.cases:
            state = self._cases[case.case_id]
            active_ns = 0
            for identity in state.active:
                start = self._attempts[identity].start_ns
                if start is not None:
                    active_ns += max(0, now - start)
            cases.append(
                {
                    "case_id": case.case_id,
                    "member_sha256s": list(case.member_sha256s),
                    "completed_members": sum(
                        self._attempts[identity].finish_ns is not None for identity in case.member_sha256s
                    ),
                    "active_members": len(state.active),
                    "finished_duration_ns": state.finished_ns,
                    "active_duration_ns": active_ns,
                    "used_ns": state.finished_ns + active_ns,
                    "limit_ns": case.limit_ns,
                }
            )
        return {
            "schema": EVIDENCE_SCHEMA,
            "status": "passed"
            if self._completed and self._failure is None
            else ("failed" if self._failure else "running"),
            "credit_eligible": self._completed and self._failure is None,
            "failure": self._failure,
            "run_id": self.plan.run_id,
            "plan_sha256": self.plan.sha256,
            "event_stream_sha256": self._event_digest.hexdigest(),
            "event_count": self._event_count,
            "retry_count": sum(max(0, attempt.start_events - 1) for attempt in self._attempts.values()),
            "last_observed_ns": self._last_observed_ns,
            "phases": [dict(row) for row in self._phase_rows],
            "attempts": attempts,
            "cases": cases,
        }
