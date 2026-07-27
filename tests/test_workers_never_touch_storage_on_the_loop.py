"""No worker coroutine may call storage directly.

The audit named five or six such call sites. A list of names goes stale the
moment someone adds the seventh, so this walks the AST of every `async def` in
`jericho/workers/` instead and fails on any direct `self.storage.…(…)` /
`self.memory_vault.…(…)` call that is not wrapped in `run_blocking`.

Why it matters, measured: every storage WRITE takes the process-wide write lock,
and one batch of embedding vectors commits in a single transaction. With such a
writer running, an inline `kv_set` from a coroutine blocked the event loop for
3.00 seconds — the loop that serves the API, the Telegram bridge and every other
worker. Reads do not take the lock but still do disk IO on that same loop.

The inventory approach is the one that already paid here: told to look for four
routes reading other accounts, walking the routes found thirteen.
"""

from __future__ import annotations

import ast
import pathlib

WORKERS = pathlib.Path(__file__).resolve().parents[1] / "jericho" / "workers"

# Attribute chains that reach the database. `self.kg` and `self.ingestion` are
# deliberately absent: they are async or already offload internally, and this
# test is about the direct path to SQLite.
STORAGE_ROOTS = {"storage", "memory_vault"}


def _is_storage_call(node: ast.Call) -> str | None:
    """`self.storage.kv_set(...)` -> "storage.kv_set", else None."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    owner = func.value
    if not isinstance(owner, ast.Attribute) or not isinstance(owner.value, ast.Name):
        return None
    if owner.value.id != "self" or owner.attr not in STORAGE_ROOTS:
        return None
    return f"{owner.attr}.{func.attr}"


def _offloaded_calls(tree: ast.AST) -> set[int]:
    """Nodes passed AS AN ARGUMENT to run_blocking — those are fine."""
    safe: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "run_blocking":
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            for inner in ast.walk(argument):
                safe.add(id(inner))
    return safe


def test_no_async_worker_calls_storage_inline():
    offenders: list[str] = []
    for path in sorted(WORKERS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        safe = _offloaded_calls(tree)
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or id(node) in safe:
                    continue
                # A call INSIDE a nested sync def is executed by whoever calls
                # that def — usually run_blocking — so it is not on the loop.
                target = _is_storage_call(node)
                if target:
                    offenders.append(f"{path.name}:{node.lineno} {function.name} -> {target}")
    assert not offenders, "storage called directly from a coroutine:\n  " + "\n  ".join(offenders)
