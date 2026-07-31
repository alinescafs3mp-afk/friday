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


@router.get("/sources", tags=["knowledge"])
async def search_sources(
    request: Request,
    q: str,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Find the SOURCE document a phrase came from, not the knowledge derived from it.

    A Knowledge Object keeps a normalised, often summarised version; the original
    characters live in the Raw Object and, until now, no index covered them —
    measured on this installation, 93% of everything ingested. This answers "which
    file did I read that in", which is a provenance question, not a recall one.

    Deliberately NOT wired into `HybridSearcher`, the agent context or any tool:
    source text is evidence about where something came from, and the place where
    resurrecting rejected material would do the most damage is an agent quoting it
    as fact. Material the reviewer marked IGNORED is excluded — see
    `storage.search_raw_objects`.
    """
    actor = _require(request, "knowledge.read")
    items = request.app.state.storage.search_raw_objects(actor.user_id, q, limit=limit)
    return {
        "query": q,
        "items": items,
        "count": len(items),
        # Said out loud rather than implied: a source search that finds nothing is
        # not proof the phrase was never ingested, only that it is not in reachable
        # material.
        "excludes": "ignored",
    }


@router.get("/tags", tags=["knowledge"])
async def list_knowledge_tags(
    request: Request,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    actor = _require(request, "knowledge.read")
    items = request.app.state.storage.list_knowledge_tags(actor.user_id, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/by-date", tags=["knowledge"])
async def knowledge_by_own_date(
    request: Request,
    since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    until: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Документы, упорядоченные по СОБСТВЕННОЙ дате — материал для хроники.

    Объявлен ДО `/{knowledge_id}`: литеральный сегмент, объявленный после
    параметра пути, был бы перехвачен им и читался как идентификатор — эту
    грабку в проекте уже ловили с `/tags`.

    Берутся только объекты со своей датой из провенанса файла. Даты, упомянутые
    в тексте, для хронологии не годятся: документ может назвать десяток чужих
    дат, и поставить его в ленту по любой из них значит соврать о времени.
    """
    actor = _require(request, "knowledge.read")
    items = request.app.state.storage.list_documents_by_own_date(
        actor.user_id, since=since, until=until, limit=limit
    )
    return {"items": items, "count": len(items), "since": since, "until": until}


@router.get("/{knowledge_id}", tags=["knowledge"])
async def get_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "knowledge.read")
    item = request.app.state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Объект знания не найден")
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
        raise HTTPException(status_code=404, detail="Объект знания не найден")
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
        raise HTTPException(status_code=404, detail="Объект знания не найден")
    after = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
    _audit(request, "knowledge.delete", "knowledge_object", knowledge_id, before=before, after=after)
    return {"status": "soft_deleted"}
