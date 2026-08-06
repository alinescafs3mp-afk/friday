"""Admin API: entities, the graph view and relation candidates.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
)
from friday.api.kg import (
    _entity_audit_fingerprint,
    _public_entity_card,
    _public_relation_candidate_card,
    _relation_audit_after,
    _relation_candidate_audit_fingerprint,
)
from friday.http_errors import relation_history_http_detail
from friday.knowledge_graph import (
    _bounded_graph_count,
    _public_graph_node,
    _public_relation,
    _validated_history_snapshot_status,
    normalize_event_date,
)
from friday.storage._graph import _bounded_entity_listing_rows, _bounded_relation_candidate_rows
from friday.storage.models import RelationHistorySnapshotError, normalize_known_at
from friday.workers._blocking import run_blocking

router = APIRouter()

_MAX_ADMIN_OVERVIEW_NODES = 500
_MAX_ADMIN_OVERVIEW_EDGES = 1_600
_MAX_ADMIN_OVERVIEW_NUMBER = 1_000_000_000.0


def _public_overview_edge(
    edge: Mapping[str, Any],
    *,
    nodes_by_raw_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bounded allowlist for an asserted or co-occurrence overview edge."""

    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    if source not in nodes_by_raw_id or target not in nodes_by_raw_id:
        raise ValueError("graph overview edge refers to an unpublished endpoint")
    raw_kind = str(edge.get("kind") or "")
    if raw_kind not in {"relation", "cooccurrence"}:
        raise ValueError("graph overview edge has an unsupported kind")
    projected: dict[str, Any] = {
        "source": str(nodes_by_raw_id[source]["id"]),
        "target": str(nodes_by_raw_id[target]["id"]),
        "kind": raw_kind,
    }
    if "id" in edge and edge.get("id") is not None:
        projected["id"] = str(edge.get("id") or "")[:160]
    if "relation_type" in edge and edge.get("relation_type") is not None:
        projected["relation_type"] = str(edge.get("relation_type") or "")[:80]
    weight = edge.get("weight")
    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
        numeric_weight = float(weight)
        if math.isfinite(numeric_weight):
            projected["weight"] = max(
                -_MAX_ADMIN_OVERVIEW_NUMBER,
                min(numeric_weight, _MAX_ADMIN_OVERVIEW_NUMBER),
            )
        else:
            projected["weight"] = 0.0
    else:
        projected["weight"] = 0.0
    return projected


def _public_graph_overview(
    raw_result: Mapping[str, Any],
    *,
    normalized_as_of: str,
    normalized_known_at: str,
) -> dict[str, Any]:
    """Validate provenance, then publish a small stable overview projection."""

    if not isinstance(raw_result, Mapping):
        raise ValueError("graph overview result is not a mapping")
    if str(raw_result.get("as_of") or "") != normalized_as_of:
        raise ValueError("graph overview changed the normalized as_of boundary")
    expected_temporal_basis = "bitemporal" if normalized_known_at else "valid_time"
    if raw_result.get("temporal_basis") != expected_temporal_basis:
        raise RelationHistorySnapshotError("graph overview returned an inconsistent temporal basis")
    history_status: dict[str, Any] | None = None
    if normalized_known_at:
        history_status = _validated_history_snapshot_status(
            raw_result,
            requested_known_at=normalized_known_at,
        )
    elif str(raw_result.get("known_at") or ""):
        raise RelationHistorySnapshotError(
            "current graph overview unexpectedly returned a historical boundary"
        )
    raw_identity_basis = raw_result.get("identity_basis")
    if raw_identity_basis is not None and raw_identity_basis != "current_names":
        raise RelationHistorySnapshotError("graph overview identity basis is unsupported")

    raw_nodes = raw_result.get("nodes")
    raw_edges = raw_result.get("edges")
    if not isinstance(raw_nodes, list) or not all(isinstance(node, Mapping) for node in raw_nodes):
        raise ValueError("graph overview nodes are not a list of mappings")
    if not isinstance(raw_edges, list) or not all(isinstance(edge, Mapping) for edge in raw_edges):
        raise ValueError("graph overview edges are not a list of mappings")

    nodes_by_raw_id: dict[str, dict[str, Any]] = {}
    public_ids: set[str] = set()
    for raw_node in raw_nodes[:_MAX_ADMIN_OVERVIEW_NODES]:
        raw_id = str(raw_node.get("id") or "")
        if not raw_id or raw_id in nodes_by_raw_id:
            raise ValueError("graph overview contains a missing or duplicate node id")
        node = _public_graph_node(raw_node)
        public_id = str(node["id"])
        if not public_id or public_id in public_ids:
            raise ValueError("bounded graph overview node ids are not unique")
        public_ids.add(public_id)
        nodes_by_raw_id[raw_id] = node

    sortable_edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        source = str(raw_edge.get("source") or "")
        target = str(raw_edge.get("target") or "")
        # Raw storage may honestly include edges incident to nodes omitted by an
        # upstream/request limit. They count as matched but cannot be published.
        if source not in nodes_by_raw_id or target not in nodes_by_raw_id:
            continue
        sortable_edges.append(_public_overview_edge(raw_edge, nodes_by_raw_id=nodes_by_raw_id))
    sortable_edges.sort(
        key=lambda edge: (
            -float(edge.get("weight") or 0.0),
            str(edge.get("kind") or ""),
            str(edge.get("relation_type") or "").casefold(),
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
            str(edge.get("id") or ""),
        )
    )
    published_edges = sortable_edges[:_MAX_ADMIN_OVERVIEW_EDGES]
    published_nodes = list(nodes_by_raw_id.values())
    nodes_matched = _bounded_graph_count(
        raw_result.get("nodes_matched_at_least"),
        len(raw_nodes),
    )
    edges_matched = _bounded_graph_count(
        raw_result.get("edges_matched_at_least"),
        len(raw_edges),
    )
    upstream_nodes_truncated = raw_result.get("nodes_truncated", False)
    upstream_edges_truncated = raw_result.get("edges_truncated", False)
    if not isinstance(upstream_nodes_truncated, bool) or not isinstance(upstream_edges_truncated, bool):
        raise ValueError("graph overview truncation metadata is not boolean")
    if upstream_nodes_truncated and nodes_matched <= len(raw_nodes):
        nodes_matched = min(len(raw_nodes) + 1, int(_MAX_ADMIN_OVERVIEW_NUMBER))
    if upstream_edges_truncated and edges_matched <= len(raw_edges):
        edges_matched = min(len(raw_edges) + 1, int(_MAX_ADMIN_OVERVIEW_NUMBER))
    total = _bounded_graph_count(raw_result.get("total"), len(raw_nodes))
    result: dict[str, Any] = {
        "nodes": published_nodes,
        "edges": published_edges,
        "shown": len(published_nodes),
        "total": total,
        "nodes_matched_at_least": nodes_matched,
        "nodes_truncated": upstream_nodes_truncated or nodes_matched > len(published_nodes),
        "edges_matched_at_least": edges_matched,
        "edges_truncated": upstream_edges_truncated or edges_matched > len(published_edges),
        "as_of": normalized_as_of,
        "known_at": normalized_known_at,
        "identity_basis": "current_names",
        "temporal_basis": expected_temporal_basis,
    }
    if history_status:
        result.update(history_status)
    return result


def _normalize_graph_boundaries(as_of: str = "", known_at: str = "") -> tuple[str, str]:
    """Parse only temporal request input, leaving internal ValueError as 500."""

    normalized_as_of = ""
    if str(as_of or "").strip():
        try:
            normalized_as_of = normalize_event_date(as_of)[0]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректная дата as_of") from exc
    normalized_known_at = ""
    if str(known_at or "").strip():
        try:
            normalized_known_at = normalize_known_at(known_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректная граница known_at") from exc
    return normalized_as_of, normalized_known_at


@router.get("/entities")
async def list_all_entities(
    request: Request,
    user_id: str | None = None,
    entity_type: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    if entity_type:
        from friday.storage.models import EntityType

        try:
            parsed_type = EntityType(entity_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Недопустимый тип сущности") from exc
    else:
        parsed_type = None
    _audit_cross_tenant_read(request, "admin.entities.read", target)
    storage = _services(request).storage
    bounded = min(limit, 200)
    rows = await run_blocking(
        _bounded_entity_listing_rows,
        storage,
        target,
        entity_types=(parsed_type.value,) if parsed_type is not None else (),
        limit=bounded + 1,
        offset=offset,
    )
    total = await run_blocking(storage.count_entities, target, parsed_type)
    items = [_public_entity_card(item) for item in rows[:bounded]]
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        # A real total, from the same filters — `count` is len(items) and on a full
        # page equals the limit, which the screen could not tell from «that is all».
        "total": total,
        "matched_at_least": total,
        "truncated": offset + len(items) < total,
        "limit": bounded,
        "offset": offset,
    }


@router.post("/entities")
async def create_entity_admin(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    from friday.storage.models import EntityType

    try:
        entity = _services(request).kg.create_entity(
            target,
            str(body.get("name") or ""),
            EntityType(str(body.get("entity_type") or "other")),
            aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else [],
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.entity.create",
        "entity",
        entity.get("id"),
        after=_entity_audit_fingerprint(entity),
    )
    return {"entity": _public_entity_card(entity)}


@router.patch("/entities/{entity_id}")
async def update_entity_admin(entity_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    state = _services(request)
    before = state.kg.get_entity(entity_id, target)
    if not before:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    fields = {key: body[key] for key in ("name", "entity_type", "aliases", "description") if key in body}
    if not fields:
        raise HTTPException(status_code=400, detail="В запросе нет изменяемых полей сущности")
    try:
        after = state.kg.update_entity(target, entity_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if after is None:
        raise HTTPException(status_code=404, detail="Объект удалён — править нечего")
    audit_after = _entity_audit_fingerprint(after)
    audit_after["changed_fields"] = sorted(fields)
    _audit(
        request,
        "admin.entity.update",
        "entity",
        entity_id,
        before=_entity_audit_fingerprint(before),
        after=audit_after,
    )
    return {"entity": _public_entity_card(after)}


@router.delete("/entities/{entity_id}")
async def delete_entity_admin(entity_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    _protect_owner_target(request, user_id)
    state = _services(request)
    before = state.kg.get_entity(entity_id, user_id)
    if not before or not state.kg.delete_entity(user_id, entity_id):
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    _audit(
        request,
        "admin.entity.delete",
        "entity",
        entity_id,
        before=_entity_audit_fingerprint(before),
    )
    return {"status": "soft_deleted"}


@router.get("/graph")
async def graph_overview(
    request: Request,
    user_id: str,
    limit: int = Query(120, ge=10, le=500),
    entity_types: str = "",
    relation_types: str = "",
    only_relations: bool = False,
    min_weight: int = Query(1, ge=1, le=50),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    as_of: str = "",
    known_at: str = Query("", max_length=64),
    search: str = "",
    hide_isolates: bool = False,
) -> dict[str, Any]:
    """Связная картина графа целиком — то, что можно нарисовать.

    Off the event loop: на большом графе это соединение таблицы связей с самой
    собой, а соседний маршрут подграфа уже стоил пяти минут блокировки, когда
    считался синхронно.

    Фильтры приходят строками через запятую и сужают ОТБОР узлов в запросе, а не
    рисование: отобрав сто самых связанных сущностей и отсеяв их в браузере, вид
    показал бы «людей — трое» там, где людей в архиве четыре тысячи.
    """
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.graph.read", user_id, scope="overview")
    normalized_as_of, normalized_known_at = _normalize_graph_boundaries(as_of, known_at)
    try:
        result = await run_blocking(
            _services(request).storage.graph_overview,
            user_id,
            limit=limit,
            entity_types=[item for item in entity_types.split(",") if item.strip()],
            relation_types=[item for item in relation_types.split(",") if item.strip()],
            only_relations=only_relations,
            min_weight=min_weight,
            min_confidence=min_confidence,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
            search=search,
            hide_isolates=hide_isolates,
        )
        return _public_graph_overview(
            result,
            normalized_as_of=normalized_as_of,
            normalized_known_at=normalized_known_at,
        )
    except RelationHistorySnapshotError as exc:
        raise HTTPException(status_code=400, detail=relation_history_http_detail(exc)) from exc


@router.get("/graph/{entity_id}")
async def graph(
    entity_id: str,
    request: Request,
    user_id: str,
    depth: int = Query(2, ge=0, le=5),
    entity_types: str = "",
    relation_types: str = "",
    min_weight: float = Query(0.0, ge=0.0, le=1.0),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    as_of: str = "",
    known_at: str = Query("", max_length=64),
) -> dict[str, Any]:
    """Окрестность узла — с теми же фильтрами, что и общий вид.

    До этого маршрут не принимал НИ ОДНОГО фильтра: человек выбирал «только
    люди», переключался с общей картины на окрестность узла и молча получал всё.
    Молча — худшая часть: вид выглядел отфильтрованным.

    `as_of` показывает картину на дату; отменённая с тех пор связь в неё
    возвращается, потому что тогда она была верна.
    `known_at` — отдельная transaction-time граница: что к тому моменту уже
    успело попасть в граф. Storage возвращает нормализованную границу и floor.
    """

    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.graph.read", user_id, entity_id=entity_id, depth=depth)
    normalized_as_of, normalized_known_at = _normalize_graph_boundaries(as_of, known_at)
    try:
        return await run_blocking(
            _services(request).kg.get_entity_graph,
            user_id,
            entity_id,
            depth,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
            entity_types=[item for item in entity_types.split(",") if item.strip()],
            relation_types=[item for item in relation_types.split(",") if item.strip()],
            min_weight=min_weight,
            min_confidence=min_confidence,
        )
    except RelationHistorySnapshotError as exc:
        raise HTTPException(status_code=400, detail=relation_history_http_detail(exc)) from exc


@router.get("/relation-candidates")
async def list_relation_candidates(
    request: Request,
    user_id: str,
    status: str | None = "suggested",
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.relations.read", user_id)
    storage = _services(request).storage
    bounded_limit = max(1, min(int(limit), 500))
    bounded_offset = max(0, int(offset))

    def _collect() -> tuple[list[dict[str, Any]], int]:
        rows = _bounded_relation_candidate_rows(
            storage,
            user_id,
            status=status or None,
            limit=bounded_limit + 1,
            offset=bounded_offset,
        )
        total = storage.count_relation_candidates(user_id, status=status or None)
        return rows, total

    try:
        rows, total = await run_blocking(_collect)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = [_public_relation_candidate_card(item) for item in rows[:bounded_limit]]
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "matched_at_least": total,
        "truncated": bounded_offset + len(items) < total,
    }


@router.post("/relation-candidates/bulk-review")
async def bulk_review_relation_candidates(request: Request) -> dict[str, Any]:
    """Review a bounded candidate batch while reporting partial failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    status = str(body.get("status") or "").casefold()
    candidate_ids = body.get("candidate_ids")
    if status not in {"accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status должен быть accepted или rejected")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids должен быть непустым списком")
    unique_ids = list(dict.fromkeys(str(item) for item in candidate_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="За раз можно разобрать не больше 200 кандидатов связей")

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for candidate_id in unique_ids:
        try:
            result = await run_blocking(
                _services(request).kg.review_relation_candidate,
                user_id,
                candidate_id,
                status,
                reviewed_by=request.state.actor.own_id,
            )
        except ValueError as exc:
            skipped.append({"id": candidate_id, "reason": str(exc)})
            continue
        if not result:
            skipped.append({"id": candidate_id, "reason": "not_found"})
            continue
        public_result = _public_relation_candidate_card(result)
        changed.append(public_result)
        _audit(
            request,
            f"admin.relation_candidate.{status}",
            "relation_candidate",
            candidate_id,
            after=_relation_candidate_audit_fingerprint(result),
        )
    return {
        "user_id": user_id,
        "status": status,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.post("/relations/{relation_id}/invalidate")
async def invalidate_relation(relation_id: str, request: Request) -> dict[str, Any]:
    """Связь перестала быть верной — но она БЫЛА, и это остаётся в архиве.

    Отдельно от мягкого удаления: то говорит «этого не было», это — «было и
    кончилось». Дата окончания приходит от человека (`valid_to`), а дата записи
    ставится сама: без второй нельзя восстановить, что система считала верным
    неделю назад, и почему тогда ответила именно так.
    """

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    try:
        result = await run_blocking(
            _services(request).kg.invalidate_relation,
            user_id,
            relation_id,
            valid_to=str(body.get("valid_to") or ""),
            superseded_by=str(body.get("superseded_by") or ""),
            reason=str(body.get("reason") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Связь не найдена")
    public_result = _public_relation(result)
    _audit(
        request,
        "admin.relation.invalidate",
        "relation",
        relation_id,
        after=_relation_audit_after(public_result),
    )
    return {"item": public_result}


@router.post("/relation-candidates/{candidate_id}/review")
async def review_relation_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    status = str(body.get("status") or "").casefold()
    try:
        result = await run_blocking(
            _services(request).kg.review_relation_candidate,
            user_id,
            candidate_id,
            status,
            reviewed_by=request.state.actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Кандидат связи не найден")
    _audit(
        request,
        f"admin.relation_candidate.{status}",
        "relation_candidate",
        candidate_id,
        after=_relation_candidate_audit_fingerprint(result),
    )
    return {"item": _public_relation_candidate_card(result)}
