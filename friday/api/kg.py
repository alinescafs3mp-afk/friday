"""Knowledge-graph HTTP routes.

Eighteen of the application's forty-nine endpoints, previously declared as nested
closures inside a 1295-line ``create_app``. Moved verbatim: same paths, methods,
capabilities, bodies and responses — only the decorator target changed, with the
``/api/kg`` prefix lifted onto the router. ``tests/test_route_inventory.py`` pins the
whole HTTP surface so the move is verified wholesale, not endpoint by endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from friday.api.deps import _audit, _parse_json_float, _request_json, _require
from friday.storage.models import EntityType, RelationType, ResolutionStatus
from friday.workers._blocking import run_blocking

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])


@router.get("/stats", tags=["knowledge-graph"])
async def graph_stats(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    state = request.app.state
    stats = state.kg.get_stats(actor.user_id)
    channel_id = str(
        request.headers.get("x-friday-chat") or request.headers.get("x-jericho-chat") or ""
    ).strip()
    if channel_id:
        session = state.storage.get_channel_session(
            actor.user_id,
            "telegram",
            channel_id,
        )
        stats["interaction_mode"] = str((session or {}).get("mode") or "dialogue")
    if state.kg.is_empty(actor.user_id):
        stats["bootstrap_suggestions"] = state.kg.get_bootstrap_suggestions(actor.user_id)
    return stats


@router.get("/entities", tags=["knowledge-graph"])
async def list_entities(
    request: Request,
    entity_type: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=5000),
) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    try:
        parsed_type = EntityType(entity_type) if entity_type else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый тип сущности") from exc
    state = request.app.state
    if q and q.strip():
        # Name lookup for browse surfaces: exact/alias/token matches only.
        matches = state.kg.search_entities(actor.user_id, q.strip(), limit=min(limit, 25))
        if parsed_type:
            matches = [item for item in matches if item.get("entity_type") == parsed_type.value]
        return {"items": matches, "count": len(matches)}
    items = state.kg.list_entities(actor.user_id, parsed_type, limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/entities", tags=["knowledge-graph"])
async def create_entity(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        entity_type = EntityType(str(body.get("entity_type") or "other"))
        entity = request.app.state.kg.create_entity(
            actor.user_id,
            str(body.get("name") or ""),
            entity_type,
            aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else [],
            description=str(body.get("description") or ""),
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "entity.create", "entity", entity.get("id"), after=entity)
    return {"entity": entity}


@router.get("/containers", tags=["knowledge-graph"])
async def list_containers(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    items = request.app.state.kg.list_containers(actor.user_id)
    return {"items": items, "count": len(items)}


@router.post("/containers", tags=["knowledge-graph"])
async def create_container(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        container = request.app.state.kg.create_container(
            actor.user_id,
            str(body.get("name") or ""),
            kind=str(body.get("kind") or EntityType.COLLECTION.value),
            parent_id=str(body.get("parent_id") or "") or None,
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "container.create", "entity", container.get("id"), after=container)
    return {"container": container}


@router.get("/entity-profile", tags=["knowledge-graph"])
async def entity_profile_by_name(name: str, request: Request) -> dict[str, Any]:
    """Object-view surface (spec v3 §6) reachable by NAME, not id — the shape a
    Telegram command or a human search box has, unlike `GET /entities/{id}`.

    Same `find_entity` best-match used by the agent's `entity_lookup` tool and
    the same `kg.entity_profile` composition, so both surfaces show the exact
    same thing for the same name — see `KnowledgeGraph.entity_profile`'s
    docstring.
    """
    actor = _require(request, "kg.read")
    kg = request.app.state.kg
    # Через поток, как соседние маршруты этого же файла: на широкой сущности
    # профиль — это несколько SQL по 22 тысячам связей, и в `async def` они
    # держали бы event loop, то есть ВСЕХ остальных пользователей.
    entity = await run_blocking(kg.find_entity, actor.user_id, name)
    if not entity:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    profile = await run_blocking(kg.entity_profile, entity["id"], actor.user_id)
    return {"entity": entity, **profile}


@router.get("/entities/{entity_id}", tags=["knowledge-graph"])
async def get_entity(entity_id: str, request: Request) -> dict[str, Any]:
    """Сущность с её связями, документами и историей версий.

    Та же болезнь, что чинили у `/entity-profile`, жила и здесь: синхронные
    запросы в `async def` (то есть остановка event loop для ВСЕХ) и список
    документов, выбранный как `k.*` — с полными телами. Замерено на копии боевой
    базы: 8.66 МБ JSON на самой широкой сущности, 79 мс SQL плюс 61 мс сериализации,
    и всё это ради восьми заголовков, которые печатает вызывающий.
    """
    actor = _require(request, "kg.read")
    state = request.app.state
    entity = await run_blocking(state.kg.get_entity, entity_id, actor.user_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    relations = await run_blocking(state.kg.get_entity_relations, entity_id, actor.user_id)
    knowledge = await run_blocking(
        state.storage.get_entity_knowledge_cards, actor.user_id, entity_id, limit=50
    )
    versions = await run_blocking(state.storage.list_entity_versions, entity_id, actor.user_id)
    return {
        "entity": entity,
        "relations": relations,
        "knowledge": knowledge,
        "versions": versions,
    }


@router.patch("/entities/{entity_id}", tags=["knowledge-graph"])
async def update_entity(entity_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    state = request.app.state
    before = state.kg.get_entity(entity_id, actor.user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    # An allow-list, not the raw body. `update_entity(user_id, entity_id, **body)`
    # splatted whatever JSON arrived into a call whose first two parameters are
    # named `user_id` and `entity_id`, so `{"user_id": ...}` raised TypeError —
    # a 500 from a request the caller was entitled to make — and every other
    # unknown key was silently accepted and dropped, which reads as success.
    # Same set the admin route already uses, plus `metadata`.
    fields = {
        key: body[key] for key in ("name", "entity_type", "aliases", "description", "metadata") if key in body
    }
    if not fields:
        raise HTTPException(status_code=400, detail="В запросе нет изменяемых полей сущности")
    try:
        after = state.kg.update_entity(actor.user_id, entity_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if after is None:
        # `update_entity` отдаёт None для удалённой сущности (и для надгробия
        # после слияния). Прежде это возвращалось как 200 с `entity: null`: чат
        # рапортовал «Тип изменён», а в аудит уходила строка о правке, которой не
        # было. Отказ должен быть отказом.
        raise HTTPException(status_code=404, detail="Объект удалён — править нечего")
    _audit(request, "entity.update", "entity", entity_id, before=before, after=after)
    return {"entity": after}


@router.delete("/entities/{entity_id}", tags=["knowledge-graph"])
async def delete_entity(entity_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    state = request.app.state
    before = state.kg.get_entity(entity_id, actor.user_id)
    if not before or not state.kg.delete_entity(actor.user_id, entity_id):
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    _audit(request, "entity.delete", "entity", entity_id, before=before)
    return {"status": "soft_deleted"}


@router.post("/entities/{entity_id}/undelete", tags=["knowledge-graph"])
async def undelete_entity(entity_id: str, request: Request) -> dict[str, Any]:
    """Вернуть удалённый объект в граф.

    Удаление объявлено мягким — значит у него обязан быть обратный ход. Его не
    было: `restore` для удалённой сущности отвечает 404 (её как бы нет), `PATCH`
    менял `entity: null`, карточка по имени не открывалась. Кнопка в чате при
    этом обещала мягкость, то есть обратимость, которой не существовало.
    """
    actor = _require(request, "kg.write")
    state = request.app.state
    try:
        entity = await run_blocking(state.storage.undelete_entity, entity_id, actor.user_id)
    except ValueError as exc:
        # Текст пишется здесь, а не пробрасывается из исключения: наружу эта
        # строка идёт человеку в Telegram, а исключение — служебное и однажды
        # окажется английским.
        raise HTTPException(
            status_code=400,
            detail="Это след слияния, а не удалённый объект: его возвращают разъединением",
        ) from exc
    if entity is None:
        raise HTTPException(status_code=404, detail="Удалённый объект с таким идентификатором не найден")
    _audit(request, "entity.undelete", "entity", entity_id, after=entity)
    return {"entity": entity}


@router.post("/entities/{entity_id}/restore", tags=["knowledge-graph"])
async def restore_entity_version(entity_id: str, request: Request) -> dict[str, Any]:
    """Вернуть сущность к состоянию из снимка её версии.

    Спека v3 §2 требует, чтобы исправление сущности было ОБРАТИМЫМ. Снимки при
    каждой правке писались с самого начала (`entity_versions`), у знаний обратный
    ход есть давно (`POST /api/admin/knowledge/{id}/restore`), а у сущностей его
    не было вовсе — правка узла была дорогой в одну сторону.

    Здесь это self-service под `kg.write`, а не админская ручка: сущности правит
    их собственный владелец, и именно ему нужен откат — на корпусе, где 4349
    узлов-людей и 149 войсковых частей заведены автоматическими правилами.

    Откат создаёт НОВУЮ версию, а не перематывает историю.
    """
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        version = int(str(body.get("version")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="version должен быть целым числом") from exc
    state = request.app.state
    before = state.kg.get_entity(entity_id, actor.user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    try:
        after = await run_blocking(
            state.kg.restore_entity_version,
            actor.user_id,
            entity_id,
            version,
            reviewed_by=actor.own_id,
        )
    except LookupError as exc:
        # Текст исключения — английский и служебный («Version 7 not found for
        # ent_…»); человек читает эту строку в Telegram, поэтому наружу идёт
        # русское объяснение, а не `str(exc)`.
        raise HTTPException(status_code=404, detail="У объекта нет такой версии") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Снимок этой версии нечитаем") from exc
    if after is None:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    _audit(request, "entity.restore", "entity", entity_id, before=before, after=after)
    return {"entity": after, "restored_from_version": version}


@router.post("/relations", tags=["knowledge-graph"])
async def create_relation(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        raw_metadata = body.get("metadata")
        user_metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        relation = request.app.state.kg.create_relation(
            actor.user_id,
            str(body.get("source_entity_id") or ""),
            str(body.get("target_entity_id") or ""),
            RelationType(str(body.get("relation_type") or "related_to")),
            weight=_parse_json_float(
                body.get("weight"),
                field="weight",
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
            # Reserved provenance keys are stamped after user metadata so a
            # request body cannot forge who created the edge or how.
            metadata={**user_metadata, "created_by": actor.user_id},
            origin="api",
            valid_from=str(body.get("valid_from") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "relation.create", "relation", relation.id, after=relation.to_row())
    return {"relation": relation.to_row()}


@router.post("/link", tags=["knowledge-graph"])
async def link_knowledge(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        link = request.app.state.kg.link_knowledge_to_entity(
            str(body.get("knowledge_object_id") or ""),
            str(body.get("entity_id") or ""),
            actor.user_id,
            confidence=_parse_json_float(
                body.get("confidence"),
                field="confidence",
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
            status=str(body.get("status") or "accepted"),
            reviewed_by=actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "knowledge.entity_link", "knowledge_entity_link", link.get("id"), after=link)
    return {"link": link}


@router.get("/graph/{entity_id}", tags=["knowledge-graph"])
async def entity_graph(
    entity_id: str,
    request: Request,
    depth: int = Query(2, ge=0, le=5),
    as_of: str = Query("", description="Картина на дату ГГГГ-ММ-ДД: что было верно тогда"),
) -> dict[str, Any]:
    """Окрестность узла; `as_of` показывает картину на названную дату.

    Отменённая связь при заданной дате возвращается: «кончилось» — это не
    «не было», и различить их можно только спросив про конкретный день.
    """

    actor = _require(request, "kg.read")
    try:
        graph = request.app.state.kg.get_entity_graph(actor.user_id, entity_id, depth, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not graph.get("nodes"):
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    return graph


@router.post("/resolutions/detect", tags=["knowledge-graph"])
async def detect_duplicates(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    report = await run_blocking(
        request.app.state.kg.resolver.sweep_duplicates, actor.user_id, min_confidence=0.55
    )
    # Предложения — из таблицы, а не из того, до чего дошёл этот тик: список и
    # `GET /resolutions/pending` обязаны говорить одно и то же.
    pending = await run_blocking(request.app.state.kg.resolver.get_pending_resolutions, actor.user_id)
    return {"items": pending, "count": len(pending), "scan": report}


@router.get("/resolutions", tags=["knowledge-graph"])
async def list_resolutions(request: Request, status: str | None = None) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    try:
        parsed = ResolutionStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус объединения") from exc
    storage = request.app.state.storage
    items = await run_blocking(storage.list_resolution_candidates, actor.user_id, parsed)
    total = await run_blocking(storage.count_resolution_candidates, actor.user_id, parsed)
    return {"items": items, "count": len(items), "total": total}


@router.get("/resolutions/pending", tags=["knowledge-graph"])
async def pending_resolutions(request: Request) -> dict[str, Any]:
    # Enriched with both entities' names and link/relation counts so review
    # surfaces (e.g. the Telegram /merges flow) can show a human-readable pair.
    actor = _require(request, "kg.read")
    # Off the event loop: enrichment is six queries per candidate, and the whole
    # request measured 317 seconds at 5000 entities — synchronously, inside an
    # `async def`, so nothing else was served for its duration.
    items = await run_blocking(request.app.state.kg.resolver.get_pending_resolutions, actor.user_id)
    total = await run_blocking(
        request.app.state.storage.count_resolution_candidates, actor.user_id, ResolutionStatus.SUGGESTED
    )
    return {"items": items, "count": len(items), "total": total}


@router.post("/resolutions/{candidate_id}/accept", tags=["knowledge-graph"])
async def accept_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.merge")
    body = await _request_json(request)
    try:
        merged = request.app.state.kg.resolver.accept_resolution(
            candidate_id,
            actor.user_id,
            target_entity_id=body.get("target_entity_id"),
            resolved_by=actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "entity.merge", "resolution", candidate_id, after=merged)
    return {"result": merged}


@router.post("/resolutions/{candidate_id}/reject", tags=["knowledge-graph"])
async def reject_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.merge")
    try:
        request.app.state.kg.resolver.reject_resolution(
            candidate_id,
            actor.user_id,
            resolved_by=actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(request, "entity.merge_rejected", "resolution", candidate_id)
    return {"status": "rejected"}


@router.get("/merges", tags=["knowledge-graph"])
async def list_merges(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Accepted merges for the current tenant, newest first.

    Each row carries a transfer set when the merge was recorded after #51;
    those can be undone via POST /merges/{id}/undo. Older empty-transfer rows
    are listed but refuse undo rather than inventing ownership.
    """
    actor = _require(request, "kg.read")
    items = await run_blocking(request.app.state.storage.list_merge_history, actor.user_id, limit=limit)
    for item in items:
        item["undoable"] = bool(
            item.get("transfer_json") and item["transfer_json"] not in ("{}", "")
        ) and not item.get("undone_at")
    return {"items": items, "count": len(items)}


@router.post("/merges/{merge_id}/undo", tags=["knowledge-graph"])
async def undo_merge(merge_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.merge")
    try:
        result = await run_blocking(
            request.app.state.kg.resolver.unmerge,
            actor.user_id,
            merge_id,
            undone_by=actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "entity.unmerge", "merge", merge_id, after=result)
    return {"result": result}


@router.get("/conflicts", tags=["knowledge-graph"])
async def list_own_conflicts(
    request: Request,
    status: str | None = "suggested",
    limit: int = Query(5, ge=1, le=20),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Tenant-scoped conflict queue for chat triage (Telegram /conflicts).

    Admin bulk routes live under /api/admin; this is the personal path that
    buttons and the agent tool share. Portions only — the live install holds
    hundreds of suggested rows.
    """
    actor = _require(request, "knowledge.read")
    storage = request.app.state.storage
    try:
        items = await run_blocking(
            storage.list_knowledge_conflicts,
            actor.user_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        total = await run_blocking(storage.count_knowledge_conflicts, actor.user_id, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Review aid for the near-duplicate queue: same features as the live probe
    # (Jaccard on content stems, length ratio, data-field share of the diff).
    # Hint only — decide endpoints are unchanged.
    from friday.conflict_triage import attach_conflict_hint

    def _with_hints() -> list[dict[str, Any]]:
        return [attach_conflict_hint(storage, actor.user_id, item) for item in items]

    annotated = await run_blocking(_with_hints)
    return {
        "items": annotated,
        "count": len(annotated),
        "total": total,
        "status": status,
        "truncated": len(annotated) < total,
    }


@router.post("/conflicts/{conflict_id}/decide", tags=["knowledge-graph"])
async def decide_own_conflict(conflict_id: str, request: Request) -> dict[str, Any]:
    """One decision on one suggested conflict: dismiss / keep_a / keep_b."""
    actor = _require(request, "knowledge.edit")
    body = await _request_json(request)
    decision = str(body.get("decision") or "").casefold().strip()
    if decision not in {"dismiss", "keep_a", "keep_b"}:
        raise HTTPException(status_code=400, detail="decision должен быть dismiss, keep_a или keep_b")
    kg = request.app.state.kg
    conflict = await run_blocking(kg.storage.get_knowledge_conflict, actor.user_id, conflict_id)
    if not conflict:
        raise HTTPException(status_code=404, detail="Конфликт не найден")
    if str(conflict.get("status") or "") != "suggested":
        raise HTTPException(status_code=409, detail=f"Конфликт уже в статусе {conflict.get('status')}")
    try:
        if decision == "dismiss":
            result = await run_blocking(
                kg.review_conflict,
                actor.user_id,
                conflict_id,
                "dismissed",
                reviewed_by=actor.own_id,
                resolution_note="chat: dismissed",
            )
            _audit(request, "knowledge_conflict.dismissed", "knowledge_conflict", conflict_id, after=result)
            return {"status": "dismissed", "item": result}
        winner_id = (
            str(conflict["knowledge_a_id"]) if decision == "keep_a" else str(conflict["knowledge_b_id"])
        )
        result = await run_blocking(
            kg.resolve_conflict,
            actor.user_id,
            conflict_id,
            winner_id,
            reviewed_by=actor.own_id,
            resolution_note=f"chat: {decision}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "knowledge_conflict.resolved", "knowledge_conflict", conflict_id, after=result)
    return {"status": "resolved", "winner_id": winner_id, "item": result}


@router.get("/timeline", tags=["knowledge-graph"])
async def event_timeline(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    try:
        events = request.app.state.kg.timeline(actor.user_id, start=start, end=end, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": events, "count": len(events), "start": start, "end": end}


@router.post("/entities/{entity_id}/time", tags=["knowledge-graph"])
async def set_event_time(entity_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    occurred_at = str(body.get("occurred_at") or "").strip()
    if not occurred_at:
        raise HTTPException(status_code=400, detail="Нужен occurred_at")
    occurred_end_value = body.get("occurred_end")
    occurred_end = str(occurred_end_value).strip() if occurred_end_value else None
    precision_value = body.get("precision")
    precision = str(precision_value).strip() if precision_value else None
    try:
        record = request.app.state.kg.set_event_time(
            actor.user_id,
            entity_id,
            occurred_at,
            occurred_end=occurred_end,
            precision=precision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "entity.time_set", "entity", entity_id, after=record)
    return {"event_time": record}
