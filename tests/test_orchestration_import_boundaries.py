"""Fresh interpreters expose import cycles otherwise hidden by pytest ordering."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    (
        "friday.orchestration.contracts",
        "friday.orchestration.coding_worker_limits",
        "friday.organs.coding.worker_spawn",
    ),
)
def test_leaf_import_does_not_load_router_or_application(module: str) -> None:
    # Clean-wheel certification must inspect the installed package, not source.
    installed = os.environ.get("FRIDAY_QUALITY_GATE_INSTALLED_SITE")
    root = Path(installed).resolve(strict=True) if installed else Path(__file__).resolve().parents[1]
    code = (
        "import importlib,sys;sys.path.insert(0,sys.argv[1]);"
        "importlib.import_module(sys.argv[2]);"
        "assert 'friday.orchestration.router' not in sys.modules;"
        "assert 'friday.server' not in sys.modules;"
        "assert 'friday.agent_runtime' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", code, str(root), module], check=True, timeout=10)


def test_public_router_exports_retain_their_original_identity() -> None:
    import friday.orchestration as package
    from friday.orchestration import router

    for name in package._ROUTER_EXPORTS:
        assert getattr(package, name) is getattr(router, name)
        assert name in dir(package)
    with pytest.raises(AttributeError):
        _ = package.not_an_export
