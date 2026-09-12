"""Execution-worker hooks for the parent-owned, first-attempt deadline ledger."""

from __future__ import annotations

import os
import stat
from typing import Any

import pytest

from tools.quality_gate_deadlines import EventWriter
from tools.quality_gate_phase import FIFO_OPTION, PHASE_OPTION, PLAN_OPTION, RUN_OPTION


def pytest_addoption(parser: Any) -> None:
    for option in (FIFO_OPTION, RUN_OPTION, PLAN_OPTION, PHASE_OPTION):
        parser.addoption(option, action="store", default="")


def pytest_sessionstart(session: Any) -> None:
    config = session.config
    values = [config.getoption(name) for name in (FIFO_OPTION, RUN_OPTION, PLAN_OPTION, PHASE_OPTION)]
    if not all(isinstance(value, str) and value for value in values):
        raise RuntimeError("gate_deadline_hook_configuration_missing")
    worker = getattr(config, "workerinput", None)
    if worker is None and config.getoption("numprocesses", default=0):
        return  # controller never attests to worker execution
    descriptor = os.open(values[0], os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISFIFO(opened.st_mode) or opened.st_uid != os.getuid() or opened.st_mode & 0o077:
            raise RuntimeError("gate_deadline_hook_fifo_unsafe")
        # Blocking single writes are <= PIPE_BUF. A stalled parent cannot mint
        # success: it owns the command ceiling and always requires both events.
        os.set_blocking(descriptor, True)
        config._friday_deadline_writer = EventWriter(
            descriptor,
            run_id=values[1],
            plan_sha256=values[2],
            phase=values[3],
            worker=worker["workerid"] if worker is not None else "serial",
        )
    except BaseException:
        os.close(descriptor)
        raise


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: Any, nextitem: Any) -> Any:
    writer = getattr(item.config, "_friday_deadline_writer", None)
    if writer is None:
        raise RuntimeError("gate_deadline_hook_writer_missing")
    writer.start(item.nodeid)
    try:
        yield
    finally:
        writer.finish(item.nodeid)


def pytest_unconfigure(config: Any) -> None:
    writer = getattr(config, "_friday_deadline_writer", None)
    if writer is not None:
        del config._friday_deadline_writer
        os.close(writer.descriptor)
