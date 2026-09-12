from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from tools import quality_gate_deadlines as q

NANO = 1_000_000_000
ROOT = Path(__file__).resolve().parents[1]
_EVENT_PRODUCER = (
    "import sys,time;sys.path.insert(0,sys.argv.pop(1));"
    "from tools.quality_gate_deadlines import EventWriter;"
    "writer=EventWriter(int(sys.argv[1]),run_id=sys.argv[2],plan_sha256=sys.argv[3],"
    "phase=sys.argv[4],worker=sys.argv[5]);"
    "writer.start(sys.argv[6]);"
    "time.sleep(float(sys.argv[7]));"
    "writer.finish(sys.argv[6])"
)


def producer(
    write_fd: int, sealed: q.DeadlinePlan, worker: str, nodeid: str, delay: float
) -> subprocess.Popen:
    return subprocess.Popen(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            _EVENT_PRODUCER,
            str(ROOT),
            str(write_fd),
            sealed.run_id,
            sealed.sha256,
            "non-ui",
            worker,
            nodeid,
            str(delay),
        ),
        cwd=ROOT,
        pass_fds=(write_fd,),
    )


def plan(*, cross_phase: bool = False) -> q.DeadlinePlan:
    second_phase = "ui" if cross_phase else "non-ui"
    phases = ("non-ui", "ui") if cross_phase else ("non-ui",)
    return q.seal_plan(
        run_id="run-001",
        phases=phases,
        nodes=(
            q.NodeDeadline("tests/a.py::test_a", "non-ui", 1),
            q.NodeDeadline("tests/b.py::test_b", second_phase, 1),
        ),
        cases=(
            q.CaseDeadline("CASE-SHARED-A", ("tests/a.py::test_a", "tests/b.py::test_b"), 1),
            q.CaseDeadline("CASE-SHARED-B", ("tests/a.py::test_a",), 1),
        ),
    )


def event(
    sealed: q.DeadlinePlan,
    nodeid: str,
    kind: str,
    when: int,
    sequence: int,
    *,
    phase: str = "non-ui",
    worker: str = "gw0",
    run_id: str | None = None,
    plan_sha256: str | None = None,
) -> bytes:
    return q.encode_event(
        q.DeadlineEvent(
            run_id or sealed.run_id,
            plan_sha256 or sealed.sha256,
            phase,
            worker,
            sequence,
            kind,
            q.nodeid_sha256(nodeid),
            when,
        )
    )


def feed(ledger: q.ParentDeadlineLedger, raw: bytes, observed: int | None = None) -> None:
    decoded = q.decode_event(raw)
    ledger.feed(raw, decoded.timestamp_ns if observed is None else observed)


def finish_two_node_phase(
    ledger: q.ParentDeadlineLedger,
    sealed: q.DeadlinePlan,
    *,
    starts: tuple[int, int],
    finishes: tuple[int, int],
) -> None:
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", starts[0], 1, worker="gw0"))
    feed(ledger, event(sealed, "tests/b.py::test_b", "start", starts[1], 1, worker="gw1"))
    feed(ledger, event(sealed, "tests/a.py::test_a", "finish", finishes[0], 2, worker="gw0"))
    feed(ledger, event(sealed, "tests/b.py::test_b", "finish", finishes[1], 2, worker="gw1"))


def test_plan_is_exact_bounded_and_digest_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    first = plan()
    assert first.sha256 == plan().sha256
    assert tuple(node.nodeid for node in first.nodes) == (
        "tests/a.py::test_a",
        "tests/b.py::test_b",
    )
    assert len(first.node_by_digest) == 2

    with pytest.raises(q.DeadlineError, match="plan_node_limit_invalid"):
        q.seal_plan(
            run_id="run",
            phases=("p",),
            nodes=(q.NodeDeadline("tests/a.py::test_a", "p", True),),
            cases=(q.CaseDeadline("C", ("tests/a.py::test_a",), 1),),
        )
    with pytest.raises(q.DeadlineError, match="plan_case_member_duplicate"):
        q.seal_plan(
            run_id="run",
            phases=("p",),
            nodes=(q.NodeDeadline("tests/a.py::test_a", "p", 1),),
            cases=(q.CaseDeadline("C", ("tests/a.py::test_a", "tests/a.py::test_a"), 1),),
        )

    monkeypatch.setattr(q, "nodeid_sha256", lambda _nodeid: "0" * 64)
    with pytest.raises(q.DeadlineError, match="plan_node_digest_collision"):
        q.seal_plan(
            run_id="run",
            phases=("p",),
            nodes=(
                q.NodeDeadline("tests/a.py::test_a", "p", 1),
                q.NodeDeadline("tests/b.py::test_b", "p", 1),
            ),
            cases=(q.CaseDeadline("C", ("tests/a.py::test_a",), 1),),
        )


def test_ledger_rebuilds_the_seal_and_rejects_a_forged_plan() -> None:
    sealed = plan()
    forged = replace(sealed, sha256="0" * 64)
    with pytest.raises(q.DeadlineError, match="ledger_plan_invalid"):
        q.ParentDeadlineLedger(forged)


def test_codec_is_canonical_fragmentable_and_pipe_atomic() -> None:
    sealed = plan()
    raw = event(sealed, "tests/a.py::test_a", "start", 10, 1)
    assert len(raw) <= 512 and raw.endswith(b"\n")
    decoder = q.EventDecoder()
    assert decoder.feed(raw[:3]) == ()
    assert decoder.feed(raw[3:-1]) == ()
    assert decoder.feed(raw[-1:]) == (q.decode_event(raw),)
    decoder.finish()

    read_fd, write_fd = os.pipe()
    try:
        assert q.write_event(write_fd, q.decode_event(raw)) == len(raw)
        assert os.read(read_fd, 512) == raw
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_largest_valid_event_stays_below_the_portable_atomic_bound() -> None:
    token = "A" * 64
    digest = "f" * 64
    raw = q.encode_event(
        q.DeadlineEvent(
            token,
            digest,
            token,
            token,
            q.MAX_SEQUENCE,
            "finish",
            digest,
            q.MAX_TIME_NS,
        )
    )
    assert len(raw) == 441
    assert len(raw) <= q.PIPE_ATOMIC_BYTES


def test_atomic_writer_never_retries_a_short_write(monkeypatch: pytest.MonkeyPatch) -> None:
    sealed = plan()
    value = q.decode_event(event(sealed, "tests/a.py::test_a", "start", 10, 1))
    read_fd, write_fd = os.pipe()
    writes: list[bytes] = []

    def short_write(descriptor: int, raw: bytes) -> int:
        assert descriptor == write_fd
        writes.append(raw)
        return len(raw) - 1

    monkeypatch.setattr(q.os, "write", short_write)
    try:
        with pytest.raises(q.DeadlineError, match="event_short_write"):
            q.write_event(write_fd, value)
        assert writes == [q.encode_event(value)]
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "mutate,code",
    (
        (lambda obj: {**obj, "extra": 1}, "event_shape_invalid"),
        (lambda obj: {**obj, "seq": True}, "event_sequence_invalid"),
        (lambda obj: {**obj, "t_ns": True}, "event_timestamp_invalid"),
        (lambda obj: {**obj, "event": "retry"}, "event_kind_invalid"),
    ),
)
def test_codec_rejects_unknown_fields_bool_numbers_and_unknown_events(mutate, code: str) -> None:
    raw = event(plan(), "tests/a.py::test_a", "start", 10, 1)
    obj = json.loads(raw)
    changed = json.dumps(mutate(obj), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    with pytest.raises(q.DeadlineError, match=code):
        q.decode_event(changed)


def test_codec_rejects_duplicate_keys_noncanonical_and_oversized_or_missing_frames() -> None:
    sealed = plan()
    raw = event(sealed, "tests/a.py::test_a", "start", 10, 1)
    with pytest.raises(q.DeadlineError, match="event_duplicate_key"):
        q.decode_event(raw.replace(b'{"event":', b'{"event":"start","event":', 1))
    with pytest.raises(q.DeadlineError, match="event_not_canonical"):
        q.decode_event(raw.replace(b",", b", ", 1))
    with pytest.raises(q.DeadlineError, match="event_frame_oversized"):
        q.EventDecoder().feed(b"x" * 512)
    decoder = q.EventDecoder()
    decoder.feed(raw[:-1])
    with pytest.raises(q.DeadlineError, match="event_frame_missing_terminator"):
        decoder.finish()


@pytest.mark.parametrize(
    "raw_factory,code",
    (
        (lambda p: event(p, "tests/a.py::test_a", "start", 10, 1, run_id="wrong"), "ledger_wrong_run"),
        (
            lambda p: event(p, "tests/a.py::test_a", "start", 10, 1, plan_sha256="0" * 64),
            "ledger_wrong_plan",
        ),
        (lambda p: event(p, "tests/a.py::test_a", "start", 10, 1, phase="ui"), "ledger_wrong_phase"),
        (
            lambda p: q.encode_event(
                replace(q.decode_event(event(p, "tests/a.py::test_a", "start", 10, 1)), node_sha256="f" * 64)
            ),
            "ledger_unknown_node",
        ),
    ),
)
def test_ledger_rejects_wrong_identity_phase_and_unknown_node(raw_factory, code: str) -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 1)
    with pytest.raises(q.DeadlineError, match=code):
        ledger.feed(raw_factory(sealed), 10)
    assert ledger.evidence()["status"] == "failed"
    assert ledger.evidence()["credit_eligible"] is False


def test_ledger_rejects_prelaunch_future_worker_regression_and_parent_regression() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 10)
    with pytest.raises(q.DeadlineError, match="event_before_phase_launch"):
        ledger.feed(event(sealed, "tests/a.py::test_a", "start", 9, 1), 10)

    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 1)
    with pytest.raises(q.DeadlineError, match="event_from_future"):
        ledger.feed(event(sealed, "tests/a.py::test_a", "start", 11, 1), 10)

    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 1)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 10, 1), 20)
    with pytest.raises(q.DeadlineError, match="worker_time_regressed"):
        ledger.feed(event(sealed, "tests/a.py::test_a", "finish", 9, 2), 20)

    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 10)
    ledger.next_deadline_ns(20)
    with pytest.raises(q.DeadlineError, match="parent_time_regressed"):
        ledger.next_deadline_ns(19)


def test_finish_before_start_duplicate_start_and_sequence_gap_are_terminal() -> None:
    sealed = plan()
    scenarios = (
        (("finish", 1), "ledger_finish_before_start"),
        (("start", 1), ("start", 2), "ledger_duplicate_start"),
        (("start", 1), ("finish", 3), "ledger_worker_sequence_out_of_order"),
    )
    for *records, code in scenarios:
        ledger = q.ParentDeadlineLedger(sealed)
        ledger.begin_phase("non-ui", 1)
        with pytest.raises(q.DeadlineError, match=code):
            for kind, sequence in records:
                feed(ledger, event(sealed, "tests/a.py::test_a", kind, 10 + sequence, sequence))
        assert ledger.evidence()["credit_eligible"] is False


def test_one_worker_cannot_claim_two_simultaneous_attempts() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 1)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 10, 1, worker="gw0"), 20)
    with pytest.raises(q.DeadlineError, match="worker_already_active"):
        feed(ledger, event(sealed, "tests/b.py::test_b", "start", 11, 2, worker="gw0"), 20)
    assert ledger.evidence()["credit_eligible"] is False


def test_concurrent_workers_may_arrive_out_of_timestamp_order() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    feed(ledger, event(sealed, "tests/b.py::test_b", "start", 200, 1, worker="gw1"), 500)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 100, 1, worker="gw0"), 500)
    feed(ledger, event(sealed, "tests/b.py::test_b", "finish", 300, 2, worker="gw1"), 500)
    feed(ledger, event(sealed, "tests/a.py::test_a", "finish", 200, 2, worker="gw0"), 500)
    ledger.close_phase("non-ui", 500, succeeded=True)
    result = ledger.complete(500, succeeded=True)
    assert result["credit_eligible"] is True
    assert result["cases"][0]["used_ns"] == 200


def test_parallel_members_charge_at_rate_n_and_equality_is_allowed() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 100, 1, worker="gw0"))
    feed(ledger, event(sealed, "tests/b.py::test_b", "start", 200, 1, worker="gw1"))

    equality = (NANO + 300) // 2
    assert ledger.next_deadline_ns(equality) == equality + 1
    assert ledger.evidence()["cases"][0]["used_ns"] == NANO
    with pytest.raises(q.DeadlineError, match="case_deadline_exceeded"):
        ledger.next_deadline_ns(equality + 1)


def test_node_deadline_allows_equality_and_violates_one_nanosecond_later() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 100, 1))
    assert ledger.next_deadline_ns(NANO + 100) == NANO + 101
    with pytest.raises(q.DeadlineError, match="node_deadline_exceeded"):
        ledger.next_deadline_ns(NANO + 101)


def test_shared_node_charges_each_case_once_and_exact_success_needs_junit_success() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    finish_two_node_phase(ledger, sealed, starts=(100, 200), finishes=(400_000_100, 500_000_200))
    ledger.close_phase("non-ui", 500_000_300, succeeded=True)
    result = ledger.complete(500_000_400, succeeded=True)
    assert result["credit_eligible"] is True
    assert result["event_count"] == 4
    assert result["retry_count"] == 0
    assert set(result) == {
        "schema",
        "status",
        "credit_eligible",
        "failure",
        "run_id",
        "plan_sha256",
        "event_stream_sha256",
        "event_count",
        "retry_count",
        "last_observed_ns",
        "phases",
        "attempts",
        "cases",
    }
    assert set(result["attempts"][0]) == {
        "nodeid",
        "node_sha256",
        "phase",
        "limit_ns",
        "start_events",
        "start_event_index",
        "finish_event_index",
        "worker",
        "start_sequence",
        "finish_sequence",
        "start_ns",
        "finish_ns",
        "duration_ns",
    }
    assert [row["finished_duration_ns"] for row in result["cases"]] == [900_000_000, 400_000_000]

    failed = q.ParentDeadlineLedger(sealed)
    failed.begin_phase("non-ui", 0)
    finish_two_node_phase(failed, sealed, starts=(100, 200), finishes=(400_000_100, 500_000_200))
    with pytest.raises(q.DeadlineError, match="ledger_phase_failed"):
        failed.close_phase("non-ui", 500_000_300, succeeded=False)
    assert failed.evidence()["credit_eligible"] is False


def test_cross_phase_finished_total_carries_and_idle_gap_costs_zero() -> None:
    sealed = plan(cross_phase=True)
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 100, 1))
    feed(ledger, event(sealed, "tests/a.py::test_a", "finish", 400_000_100, 2))
    ledger.close_phase("non-ui", 400_000_200, succeeded=True)

    assert ledger.next_deadline_ns(50 * NANO) is None
    ledger.begin_phase("ui", 100 * NANO)
    feed(
        ledger,
        event(sealed, "tests/b.py::test_b", "start", 100 * NANO + 100, 1, phase="ui"),
    )
    assert ledger.next_deadline_ns(100 * NANO + 600_000_100) == 100 * NANO + 600_000_101
    feed(
        ledger,
        event(
            sealed,
            "tests/b.py::test_b",
            "finish",
            100 * NANO + 600_000_100,
            2,
            phase="ui",
        ),
    )
    ledger.close_phase("ui", 101 * NANO, succeeded=True)
    result = ledger.complete(101 * NANO, succeeded=True)
    assert result["cases"][0]["used_ns"] == NANO


def test_phase_close_and_completion_reject_missing_members_and_external_failure() -> None:
    sealed = plan()
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", 0)
    feed(ledger, event(sealed, "tests/a.py::test_a", "start", 1, 1))
    feed(ledger, event(sealed, "tests/a.py::test_a", "finish", 2, 2))
    with pytest.raises(q.DeadlineError, match="phase_events_missing"):
        ledger.close_phase("non-ui", 3, succeeded=True)

    ledger = q.ParentDeadlineLedger(sealed)
    ledger.abort("junit-red")
    with pytest.raises(q.DeadlineError, match="ledger_failed:junit-red"):
        ledger.complete(4, succeeded=True)
    assert ledger.evidence()["credit_eligible"] is False


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX atomic pipe writers")
def test_real_concurrent_producers_remain_atomic_under_fragmented_reads() -> None:
    nodeids = tuple(f"tests/probe.py::test_concurrent[{index}]" for index in range(12))
    sealed = q.seal_plan(
        run_id="concurrent-probe",
        phases=("non-ui",),
        nodes=tuple(q.NodeDeadline(nodeid, "non-ui", 5) for nodeid in nodeids),
        cases=(
            q.CaseDeadline("ALL", nodeids, 5),
            q.CaseDeadline("EVEN", nodeids[::2], 5),
        ),
    )
    ledger = q.ParentDeadlineLedger(sealed)
    launch = time.monotonic_ns()
    ledger.begin_phase("non-ui", launch)
    read_fd, write_fd = os.pipe()
    children: list[subprocess.Popen] = []
    statuses = []
    try:
        for index, nodeid in enumerate(nodeids):
            children.append(producer(write_fd, sealed, f"gw{index}", nodeid, (index % 3) / 1000))
        os.close(write_fd)
        write_fd = -1
        while True:
            ready, _, _ = select.select((read_fd,), (), (), 5)
            assert ready
            raw = os.read(read_fd, 37)  # deliberately split canonical frames
            if not raw:
                break
            ledger.feed(raw, time.monotonic_ns())
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
            statuses.append(child.wait(timeout=5))
    assert all(status == 0 for status in statuses)
    ledger.close_phase("non-ui", time.monotonic_ns(), succeeded=True)
    result = ledger.complete(time.monotonic_ns(), succeeded=True)
    assert result["event_count"] == 24
    assert result["retry_count"] == 0
    assert len({row["node_sha256"] for row in result["attempts"]}) == 12
    assert result["credit_eligible"] is True


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX concurrent deadline producers")
def test_real_concurrent_shared_case_expires_at_aggregate_rate_without_credit() -> None:
    nodeids = ("tests/probe.py::test_a", "tests/probe.py::test_b")
    sealed = q.seal_plan(
        run_id="aggregate-expiry-probe",
        phases=("non-ui",),
        nodes=tuple(q.NodeDeadline(nodeid, "non-ui", 5) for nodeid in nodeids),
        cases=(
            q.CaseDeadline("AGGREGATE", nodeids, 1),
            q.CaseDeadline("SHARED", (nodeids[0],), 5),
        ),
    )
    ledger = q.ParentDeadlineLedger(sealed)
    ledger.begin_phase("non-ui", time.monotonic_ns())
    read_fd, write_fd = os.pipe()
    children: list[subprocess.Popen] = []
    try:
        for index, nodeid in enumerate(nodeids):
            children.append(producer(write_fd, sealed, f"gw{index}", nodeid, 5))
        os.close(write_fd)
        write_fd = -1
        while ledger.evidence()["event_count"] < 2:
            ready, _, _ = select.select((read_fd,), (), (), 2)
            assert ready
            raw = os.read(read_fd, 37)
            assert raw
            ledger.feed(raw, time.monotonic_ns())

        while True:
            now = time.monotonic_ns()
            boundary = ledger.next_deadline_ns(now)
            assert boundary is not None
            select.select((), (), (), max(0.0, (boundary - now) / NANO))
            try:
                ledger.next_deadline_ns(time.monotonic_ns())
            except q.DeadlineError as exc:
                assert exc.code.endswith("ledger_case_deadline_exceeded")
                break
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        for child in children:
            with suppress(ProcessLookupError):
                child.send_signal(signal.SIGTERM)
        statuses = [child.wait(timeout=5) for child in children]

    evidence = ledger.evidence()
    aggregate, shared = evidence["cases"]
    assert evidence["event_count"] == 2
    assert evidence["retry_count"] == 0
    assert evidence["credit_eligible"] is False
    assert aggregate["used_ns"] > aggregate["limit_ns"]
    assert shared["used_ns"] < shared["limit_ns"]
    assert len(statuses) == 2
