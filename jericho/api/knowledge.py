"""HTTP routes for knowledge.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from jericho.api.deps import _audit, _request_json, _require

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("", tags=["knowledge"])
async def list_knowledge(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    lifecycle_stage: str | None = None,
    tag: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    actor = _require(request, "knowledge.read")
    items = request.app.state.storage.list_knowledge_objects(
        actor.user_id,
        limit=limit,
        offset=offset,
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        entity_id=entity_id,
    )
    return {"items": items, "count": len(items)}


@router.get("/tags", tags=["knowledge"])
async def list_knowledge_tags(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    actor = _require(request, "knowledge.read")
    items = request.app.state.storage.list_knowledge_tags(actor.user_id, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/{knowledge_id}", tags=["knowledge"])
async def get_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.read")
    item = request.app.state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    return {
        "item": item,
        "versions": request.app.state.storage.list_knowledge_versions(knowledge_id, actor.user_id),
        "entity_links": request.app.state.storage.list_knowledge_entity_links(
            actor.user_id,
            knowledge_object_id=knowledge_id,
            status=None,
        ),
    }


@router.patch("/{knowledge_id}", tags=["knowledge"])
async def update_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.edit")
    body = await _request_json(request)
    state = request.app.state
    before = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    allowed = {
        "title",
        "summary",
        "content",
        "tags_json",
        "importance",
        "lifecycle_stage",
        "knowledge_kind",
        "quality_score",
    }
    updates = {key: body[key] for key in allowed if key in body}
    try:
        after = state.storage.update_knowledge_fields(knowledge_id, actor.user_id, **updates)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "knowledge.update", "knowledge_object", knowledge_id, before=before, after=after)
    return {"item": after}


@router.delete("/{knowledge_id}", tags=["knowledge"])
async def delete_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.delete")
    state = request.app.state
    before = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    if not before or not state.storage.soft_delete_knowledge_object(knowledge_id, actor.user_id):
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    after = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    _audit(request, "knowledge.delete", "knowledge_object", knowledge_id, before=before, after=after)
    return {"status": "soft_deleted"}
