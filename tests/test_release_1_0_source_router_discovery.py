"""Source discovery binds each decorator to the router that owns it."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import release_1_0_acceptance as acceptance


def _write_source_tree(root: Path, routes: str) -> Path:
    package = root / "friday"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "routes.py").write_text(routes, encoding="utf-8")
    filler = "\n".join(
        f'@application.get("/api/source-filler-{index}")\nasync def filler_{index}():\n    pass\n'
        for index in range(45)
    )
    (package / "filler.py").write_text(filler, encoding="utf-8")
    return root


def test_executive_source_routes_keep_user_and_admin_prefixes_separate():
    surfaces = set(acceptance.discover_api_from_source())
    assert {
        "api:POST /api/missions",
        "api:POST /api/missions/{mission_id}/start",
        "api:POST /api/missions/{mission_id}/stop",
        "api:GET /api/admin/missions",
        "api:GET /api/admin/missions/{mission_id}",
        "api:POST /api/admin/missions/{mission_id}/cancel",
    } <= surfaces
    assert "api:POST /api/missions/{mission_id}/cancel" not in surfaces


def test_renamed_routers_bind_same_relative_route_and_preserve_empty_and_root(tmp_path):
    root = _write_source_tree(
        tmp_path,
        """
from fastapi import APIRouter

personal = APIRouter(prefix="/api/things")
operations = APIRouter(prefix="/api/admin/things")

@personal.get("")
async def list_things():
    pass

@personal.post("/")
async def create_thing_at_slash():
    pass

@personal.delete("/{thing_id}")
async def delete_personal_thing():
    pass

@operations.delete("/{thing_id}")
async def delete_admin_thing():
    pass
""",
    )
    surfaces = set(acceptance.discover_api_from_source(root))
    assert {
        "api:GET /api/things",
        "api:POST /api/things/",
        "api:DELETE /api/things/{thing_id}",
        "api:DELETE /api/admin/things/{thing_id}",
    } <= surfaces
    assert "api:DELETE /{thing_id}" not in surfaces


@pytest.mark.parametrize(
    ("routes", "error"),
    [
        (
            """
from fastapi import APIRouter
PREFIX = "/api/things"
router = APIRouter(prefix=PREFIX)

@router.get("/one")
async def one():
    pass
""",
            "api_source_router_prefix_dynamic",
        ),
        (
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/things")
ROUTE = "/one"

@router.get(ROUTE)
async def one():
    pass
""",
            "api_source_route_dynamic",
        ),
        (
            """
from fastapi import APIRouter
options = build_options()
router = APIRouter(**options)

@router.post("/hidden")
async def hidden():
    pass
""",
            "api_source_router_prefix_dynamic",
        ),
    ],
    ids=("dynamic-prefix", "dynamic-path", "expanded-keywords"),
)
def test_dynamic_source_router_identity_fails_closed(tmp_path, routes, error):
    root = _write_source_tree(tmp_path, routes)
    with pytest.raises(acceptance.AcceptanceError, match=error):
        acceptance.discover_api_from_source(root)


def test_conditional_source_only_router_decorator_is_discovered(tmp_path):
    root = _write_source_tree(
        tmp_path,
        """
from fastapi import APIRouter
router = APIRouter(prefix="/api/probe")

if False:
    @router.post("/hidden")
    async def hidden():
        pass
""",
    )
    surfaces = set(acceptance.discover_api_from_source(root))
    assert "api:POST /api/probe/hidden" in surfaces


def test_simple_router_alias_preserves_known_prefix(tmp_path):
    root = _write_source_tree(
        tmp_path,
        """
from fastapi import APIRouter
operations = APIRouter(prefix="/api/admin/things")
router = operations

@router.post("/hidden")
async def hidden():
    pass
""",
    )
    surfaces = set(acceptance.discover_api_from_source(root))
    assert "api:POST /api/admin/things/hidden" in surfaces
    assert "api:POST /hidden" not in surfaces


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/known")
router = build_router()

@router.post("/hidden")
async def hidden():
    pass
""",
            id="non-router-rebind",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/known")

def register(router):
    @router.post("/hidden")
    async def hidden():
        pass
""",
            id="parameter-shadow",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/known")
from synthetic_routes import router

@router.post("/hidden")
async def hidden():
    pass
""",
            id="import-shadow",
        ),
    ],
)
def test_ambiguous_router_binding_fails_closed(tmp_path, routes):
    root = _write_source_tree(tmp_path, routes)
    with pytest.raises(acceptance.AcceptanceError, match="api_source_router_binding_ambiguous"):
        acceptance.discover_api_from_source(root)


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
scratch = (router := build_router())

@router.post("/hidden")
def hidden():
    pass
""",
            id="assignment-walrus",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
try:
    router = APIRouter(prefix="/api/new")
    raise ValueError()
except ValueError:
    @router.post("/hidden")
    def hidden():
        pass
""",
            id="partial-try-handler",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
for iteration in (1, 2):
    @router.post("/hidden")
    def hidden():
        pass
    router = APIRouter(prefix="/api/new")
""",
            id="loop-carried-router",
        ),
    ],
)
def test_control_flow_router_mutation_fails_closed(tmp_path, routes):
    root = _write_source_tree(tmp_path, routes)
    with pytest.raises(acceptance.AcceptanceError, match="api_source_router_binding_ambiguous"):
        acceptance.discover_api_from_source(root)


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
def helper(x=(router := APIRouter(prefix="/api/new"))):
    pass

@router.post("/hidden")
def hidden():
    pass
""",
            id="definition-default-walrus",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
for i in (1, 2):
    @router.post("/hidden")
    def hidden():
        pass
    if i == 1:
        router = APIRouter(prefix="/api/new")
        continue
    router = APIRouter(prefix="/api/old")
""",
            id="continue-skips-tail-reset",
        ),
        pytest.param(
            """
from fastapi import APIRouter
router = APIRouter(prefix="/api/old")
for i in (1, 2):
    if i == 1:
        router = APIRouter(prefix="/api/new")
        break
    router = APIRouter(prefix="/api/old")

@router.post("/hidden")
def hidden():
    pass
""",
            id="break-skips-tail-reset",
        ),
    ],
)
def test_definition_and_loop_transfer_router_mutation_fails_closed(tmp_path, routes):
    root = _write_source_tree(tmp_path, routes)
    with pytest.raises(acceptance.AcceptanceError, match="api_source_router_binding_ambiguous"):
        acceptance.discover_api_from_source(root)


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param(
            'router=APIRouter(prefix="/api/old")\nfor i in (1,2):\n    @router.post("/hidden")\n    def hidden(): pass\n    if i == 1:\n        for j in ():\n            pass\n        else:\n            router=APIRouter(prefix="/api/new")\n            continue\n    router=APIRouter(prefix="/api/old")\n',
            id="nested_loop_else_continue",
        ),
        pytest.param(
            'router=APIRouter(prefix="/api/old")\nfor i in (1,2):\n    if i == 1:\n        for j in ():\n            pass\n        else:\n            router=APIRouter(prefix="/api/new")\n            break\n    router=APIRouter(prefix="/api/old")\n@router.post("/hidden")\ndef hidden(): pass\n',
            id="nested_loop_else_break",
        ),
        pytest.param(
            'router=APIRouter(prefix="/api/old")\nhelper=lambda x=(router:=APIRouter(prefix="/api/new")): None\n@router.post("/hidden")\ndef hidden(): pass\n',
            id="lambda_default_walrus",
        ),
    ],
)
def test_definition_defaults_and_nested_loop_else_do_not_keep_stale_router(tmp_path, routes):
    root = _write_source_tree(tmp_path, routes)
    with pytest.raises(acceptance.AcceptanceError, match="api_source_router_binding_ambiguous"):
        acceptance.discover_api_from_source(root)
