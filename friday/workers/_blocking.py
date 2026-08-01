"""Track blocking work that outlives the coroutine waiting for it.

``asyncio.to_thread`` hands a function to a thread and gives back an awaitable. When the
awaiting task is cancelled — which is exactly what a worker timeout does — the *await*
ends, but the thread does not: nothing in Python can interrupt a running thread. The
work carries on, holding its database connection, and the supervisor, believing the run
is over, starts the next one on schedule.

Measured on this supervisor before the guard existed: a task whose blocking call ran
seven seconds under a five-second timeout reached **two concurrent threads**, with the
task reporting ``running`` while an orphan from the previous tick was still writing.

The counter is incremented and decremented INSIDE the thread, so it reflects what is
actually still executing rather than what the event loop believes. The task name comes
from a context variable because ``to_thread`` copies the caller's context into the
thread, which is the only channel that survives the same cancellation.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Set by the supervisor around each task invocation; read inside worker threads.
current_task: contextvars.ContextVar[str] = contextvars.ContextVar("jericho_worker_task", default="")

_lock = threading.Lock()
_in_flight: dict[str, int] = {}


def in_flight(task_name: str) -> int:
    """How many blocking calls started by this task are still executing."""
    with _lock:
        return _in_flight.get(task_name, 0)


def snapshot() -> dict[str, int]:
    with _lock:
        return {name: count for name, count in _in_flight.items() if count}


def wait_until_idle(timeout: float, *, poll_interval: float = 0.05) -> dict[str, int]:
    """Block until no tracked blocking call remains, or ``timeout`` elapses.

    Returns what is still running — empty when the drain succeeded. Shutdown needs
    this because ``storage.close()`` only drains WRITERS: it takes the write lock,
    and the read path deliberately takes no lock at all, so a worker thread halfway
    through a ``SELECT`` is invisible to it. Bounding the wait by a flat constant is
    equally wrong — the budget belongs to the worker, and ``knowledge_dedup`` alone
    is allowed 600 seconds inside a 900-second tick.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        remaining = snapshot()
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(poll_interval)


def _tracked(task_name: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    if task_name:
        with _lock:
            _in_flight[task_name] = _in_flight.get(task_name, 0) + 1
    try:
        return function(*args, **kwargs)
    finally:
        if task_name:
            with _lock:
                remaining = _in_flight.get(task_name, 1) - 1
                if remaining > 0:
                    _in_flight[task_name] = remaining
                else:
                    _in_flight.pop(task_name, None)


async def run_blocking(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """``asyncio.to_thread`` that admits when its thread outlives the await.

    A drop-in replacement everywhere a worker offloads storage work. Outside a worker
    (no task in context) it behaves exactly like ``asyncio.to_thread``.
    """
    return await asyncio.to_thread(_tracked, current_task.get(), function, *args, **kwargs)
