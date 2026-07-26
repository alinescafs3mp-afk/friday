"""Authenticated administration and cross-tenant data-management API.

The 77 endpoints live in one module per area of the console (see ``_users``,
``_knowledge`` and siblings); this package owns the ``/api/admin`` prefix and,
more importantly, the order they are included in.

That order is load-bearing. FastAPI 0.139 matches in registration order, so a
parameterized path included ahead of a literal one it matches swallows the
literal — and answers 200 from the wrong handler rather than failing. Today one
such pair exists (``/knowledge/tags`` before ``/knowledge/{knowledge_id}``) and
both are in ``_knowledge``, where source order settles it.
``tests/test_route_inventory.py`` checks the whole application for that.
"""

from __future__ import annotations

from fastapi import APIRouter

from jericho.admin_api import (
    _conflicts,
    _conversations,
    _evaluation,
    _files,
    _graph,
    _inbox,
    _knowledge,
    _lifecycle,
    _maintenance,
    _overview,
    _users,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/admin", tags=["admin"])

router.include_router(_overview.router)
router.include_router(_users.router)
router.include_router(_knowledge.router)
router.include_router(_inbox.router)
router.include_router(_graph.router)
router.include_router(_conflicts.router)
router.include_router(_lifecycle.router)
router.include_router(_conversations.router)
router.include_router(_files.router)
router.include_router(_evaluation.router)
router.include_router(_maintenance.router)
