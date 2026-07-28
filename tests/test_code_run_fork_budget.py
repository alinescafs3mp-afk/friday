"""The sandbox's memory ceiling has to be a total, not a per-process allowance.

Every rlimit on Linux is per process and children inherit it as their OWN budget, so
`RLIMIT_AS = 512 MiB` bounded one process and nothing else: measured before the fix,
four forks held 1037 MiB of RSS while each reported an address-space limit of exactly
512 MiB. `RLIMIT_NPROC` is what turns per-process limits into a total, because it is
checked at fork time — but it is checked against the real UID's whole process count,
so a fixed small number would stop the executor from starting at all. The ceiling is
therefore relative to what the UID already owns.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from jericho.execution_kernel import ExecutionKernel, _count_user_tasks
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.web_surfer import WebSurfer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX")


def test_the_task_count_is_plausible():
    count = _count_user_tasks()
    assert count >= 1
    # This test process is one of them, so the count cannot be lower than that.
    assert count < 100_000


@pytest.mark.asyncio
async def test_forking_cannot_multiply_the_memory_budget(settings, storage):
    storage.ensure_user("operator", preset_key="owner")
    executable = replace(settings, code_execution_enabled=True, code_execution_timeout_sec=10)
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(executable)
    kernel = ExecutionKernel(auth, executable)
    kernel.bind_services(storage, graph, web, IngestionPipeline(executable, storage, graph))
    actor = auth.actor_for_user("operator", source="test")

    # Fork until the kernel refuses, and report how far it got. Each survivor would
    # otherwise carry its own full 512 MiB allowance.
    code = (
        "import os\n"
        "children = 0\n"
        "for _ in range(64):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    children += 1\n"
        "print(children)\n"
    )
    try:
        result = await kernel.execute("code_run", {"code": code}, actor=actor)
    finally:
        await web.close()

    assert result.success is True, result.error
    forked = int((result.data["stdout"] or "0").strip() or 0)
    assert forked < 64, "the sandbox forked freely; 64 processes is 32 GiB of allowance"
