"""Admin API: Knowledge Objects, containers and entity links.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``jericho.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter

from jericho.admin_api._deps import (
    Any,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _parse_bool,
    _parse_unit_float,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
    asyncio,
    purge_knowledge,
)
from jericho.storage.models import EntityType

router = APIRouter()


def _knowledge_fingerprint(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Enough to identify a Knowledge Object in the journal, and no content.

    `audit_log` is append-only at the DATABASE level — BEFORE UPDATE and BEFORE
    DELETE triggers `RAISE(ABORT)` — so whatever a route writes here is permanent
    beyond the reach of any later fix, purge or redaction. That makes the body of
    a personal note the last thing that belongs in it. What an investigation
    needs is which object, how big it was, and what it was called; what it does
    not need is the note itself.
    """
    if not item:
        return None
    content = str(item.get("content") or "")
    return {
        "id": item.get("id"),
        "user_id": item.get("user_id"),
        "title": str(item.get("title") or "")[:120],
        "knowledge_kind": item.get("knowledge_kind"),
        "lifecycle_stage": item.get("lifecycle_stage"),
        "version": item.get("version"),
        "content_chars": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


@router.get("/knowledge")
async def list_all_knowledge(
    request: Request,
    user_id: str | None = None,
    lifecycle_stage: str | None = None,
    tag: str | None = None,
    entity_id: str | None = None,
    # Строка поиска по заголовку, сводке и имени файла. Без неё найти документ в
    # админке нельзя: замерено на корпусе владельца — важность лежит в полосе
    # 0.66..0.72, различных дней в `updated_at` три на 1537 объектов, а два служебных
    # тега стоят на 1524 из них. То есть и сортировка, и фильтр по тегу вырождены, и
    # остаётся листание полутора тысяч строк.
    q: str | None = Query(None, max_length=200),
    # Диапазон дат, УПОМЯНУТЫХ в документе. Именно упомянутых: документ называет
    # несколько дат, и какая из них его собственная — данные не говорят. Придумывать
    # «главную» значило бы угадывать за человека.
    since: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    until: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.knowledge.read", target)
    storage = _services(request).storage
    items = storage.list_knowledge_objects(
        target,
        limit=limit,
        offset=offset,
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        entity_id=entity_id,
        query=q,
        since=since,
        until=until,
    )
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        # `count_filtered_knowledge_objects`, NOT `count_knowledge_objects`: the latter
        # counts every live object of the account and ignores tag, lifecycle and entity,
        # so on a filtered view it would report thousands over a set of twelve and the
        # pager would never reach its last page.
        "total": storage.count_filtered_knowledge_objects(
            target,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
            query=q,
            since=since,
            until=until,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/source-search")
async def search_source_text(
    request: Request,
    q: str,
    user_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Дословный поиск по ИСХОДНОМУ тексту материала — мимо ранжирования.

    Существовал только в CLI (`jericho source`), то есть был недоступен человеку,
    который живёт в админке и Telegram. А нужен он ровно тогда, когда подводит
    ранжирование: замерено на корпусе владельца — из пяти выданных поиском документов
    по существу отвечают два, и когда человек ПОМНИТ точную фразу, ему нужен не
    смысловой поиск, а дословный.

    Замерено там же: 93% загруженных знаков живут только в `raw_objects` — Knowledge
    Object несёт нормализованную, часто сокращённую версию. То есть точная фраза из
    PDF без этого пути не находилась вовсе.

    Отклонённый в Inbox материал сюда НЕ входит, и это вердикт, а не фильтр: см.
    `search_raw_objects`. Возврат его текста отменил бы решения человека — тот самый
    класс воскрешения, который в этом проекте закрывали трижды.
    """
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.source.search", target)
    items = _services(request).storage.search_raw_objects(target, q, limit=limit)
    return {
        "user_id": target,
        "query": q,
        "items": items,
        # Здесь `count` — это ДЛИНА СТРАНИЦЫ, и так и подписано. Полного числа
        # совпадений FTS не даёт без второго запроса, а показывать длину среза как
        # «найдено всего» — ровно та ложь, которую в этом проекте уже ловили.
        "count": len(items),
        "limit": limit,
    }


@router.get("/knowledge/tags")
async def list_all_knowledge_tags(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.knowledge.read", target)
    items = _services(request).storage.list_knowledge_tags(target, limit=limit)
    return {"user_id": target, "items": items, "count": len(items)}


@router.get("/containers")
async def list_all_containers(request: Request, user_id: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.containers.read", target)
    items = _services(request).kg.list_containers(target)
    return {"user_id": target, "items": items, "count": len(items)}


@router.post("/containers")
async def create_container_admin(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    try:
        container = _services(request).kg.create_container(
            target,
            str(body.get("name") or ""),
            kind=str(body.get("kind") or "collection"),
            parent_id=str(body.get("parent_id") or "") or None,
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.container.create", "entity", container.get("id"), after=container)
    return {"container": container}


@router.get("/knowledge/{knowledge_id}")
async def inspect_knowledge(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    state = _services(request)
    item = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    _audit_cross_tenant_read(request, "admin.knowledge.inspect", user_id, knowledge_id=knowledge_id)
    raw = state.storage.get_raw_object(item["raw_object_id"], user_id)
    versions = state.storage.list_knowledge_versions(knowledge_id, user_id)
    links = state.storage.list_knowledge_entity_links(
        user_id, knowledge_object_id=knowledge_id, status=None, limit=500
    )
    for link in links:
        link["evidence"] = _json_value(link.get("evidence_json"), {})
        link["entity"] = state.storage.get_entity(link["entity_id"], user_id)
    inbox = state.storage.get_inbox_by_raw(item["raw_object_id"], user_id)
    if inbox:
        inbox["suggestions"] = _json_value(inbox.get("suggestions_json"), {})
    item["metadata"] = _json_value(item.get("metadata_json"), {})
    item["tags"] = _json_value(item.get("tags_json"), [])
    if raw:
        raw["metadata"] = _json_value(raw.get("metadata_json"), {})
    return {
        "item": item,
        "raw_object": raw,
        "versions": versions,
        "entity_links": links,
        "inbox": inbox,
    }


@router.get("/knowledge/{knowledge_id}/diff")
async def knowledge_diff(
    knowledge_id: str,
    request: Request,
    user_id: str,
    from_version: int | None = Query(None, ge=1),
    to_version: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.knowledge.diff", user_id, knowledge_id=knowledge_id)
    result = _services(request).storage.diff_knowledge_versions(
        knowledge_id, user_id, from_version=from_version, to_version=to_version
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No versions to diff")
    return result


@router.post("/knowledge/{knowledge_id}/reenrich")
async def reenrich_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    try:
        result = state.ingestion.reenrich_knowledge(
            user_id,
            knowledge_id,
            apply=_parse_bool(body.get("apply", False), field="apply"),
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["applied"]:
        _audit(
            request,
            "admin.knowledge.reenrich",
            "knowledge_object",
            knowledge_id,
            before=before,
            after=result["item"],
        )
    return result


@router.post("/knowledge/{knowledge_id}/entity-links")
async def create_knowledge_entity_link(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    entity_id = str(body.get("entity_id") or "")
    status = str(body.get("status") or "accepted")
    if status not in {"suggested", "accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be suggested, accepted, or rejected")
    state = _services(request)
    try:
        link = state.kg.link_knowledge_to_entity(
            knowledge_id,
            entity_id,
            user_id,
            confidence=_parse_unit_float(body.get("confidence", 1.0), field="confidence"),
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
            status=status,
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.knowledge.entity_link.create",
        "knowledge_entity_link",
        link.get("id"),
        after=link,
    )
    return {"link": link}


@router.get("/entity-suggestions/queue")
async def entity_suggestion_queue(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Документы, у которых остались неразобранные предложения сущностей.

    Очереди не существовало, и это объясняет, почему экраном подтверждения не
    воспользовались НИ РАЗУ: проверено по живой базе — ни одна из 109 сущностей и ни
    одна из 226 связей не пришла от человека. Кандидаты считались по запросу и нигде
    не хранились, поэтому их нельзя было ни посчитать, ни показать: ни плитки на
    обзоре, ни очереди в разделе «Граф». Единственным входом было «открой конкретный
    документ и нажми Инспекция» — то есть надо было заранее знать, куда идти.
    """
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.entity_suggestions.read", target)
    items, total = _services(request).storage.list_documents_with_entity_suggestions(
        target, limit=limit, offset=offset
    )
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        "total": total,
        # Честно: это ОЦЕНКА сверху. Предложение и связь — не одно и то же, и точный
        # остаток требует пересчёта по тексту каждого документа.
        "estimate": True,
        "limit": limit,
        "offset": offset,
    }


@router.get("/knowledge/{knowledge_id}/entity-suggestions")
async def list_entity_suggestions(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    """Какие сущности этот документ предлагает, но которых в графе ещё нет.

    Считается ПО ЗАПРОСУ из текста самого объекта, а не хранится: кандидаты нигде и
    не сохранялись (в метаданных лежало только их число), а вычисление из текущего
    текста заодно не устаревает после правки документа.

    Почему это вообще понадобилось. Сущность создаётся автоматически только при
    уверенности ≥ 0.88, а два метода, дающие на реальном корпусе владельца почти всё
    (`capitalized_person_name` — 5797 кандидатов, `identifier_syntax` — 3907), стоят
    на 0.76 и 0.75. Порог поднят намеренно: в v0.99.0 замерили, что из 28
    автопринятых связей 26 давал `identifier_syntax` и вещами была четверть. То есть
    опускать порог нельзя, а другого пути кандидату в граф не было вовсе — и цепочка
    «извлекли → человек подтвердил → нашлась связь» была разомкнута на первом звене.
    """
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.entity_suggestions.read", user_id)
    state = _services(request)
    knowledge = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not knowledge or knowledge.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Knowledge object not found")

    content = str(knowledge.get("content") or knowledge.get("summary") or "")
    suggestions = await asyncio.to_thread(
        state.ingestion._entity_suggestions,  # noqa: SLF001
        user_id,
        content,
    )
    # Уже связанное не предлагается второй раз — ни принятое, ни отклонённое:
    # предложить снова то, что человек отверг, значит просить решение дважды.
    decided = {
        str(row.get("entity_id"))
        for row in state.storage.list_knowledge_entity_links(
            user_id, knowledge_object_id=knowledge_id, status=None, limit=500
        )
    }
    pending = [item for item in suggestions if str(item.get("entity_id") or "") not in decided]
    # По убыванию уверенности: на живом документе из 23 кандидатов годными были
    # единицы, а `location_preposition` (0.77) исправно принимает «в Наставлении» и
    # «в Курсе» за места. Разбирать список, где годное перемешано с шумом, человек
    # бросит на третьем экране — а порядок по уверенности ставит объявленные методы
    # (0.89–0.93) наверх бесплатно.
    pending.sort(key=lambda item: (-float(item.get("confidence") or 0.0), str(item.get("name") or "")))
    return {
        "knowledge_object_id": knowledge_id,
        "items": pending,
        "count": len(pending),
        "decided": len(decided),
    }


@router.post("/knowledge/{knowledge_id}/entities")
async def accept_entity_suggestion(knowledge_id: str, request: Request) -> dict[str, Any]:
    """Подтвердить предложенную сущность целиком — как предложена.

    Создаёт узел, если его ещё нет, ставит УТВЕРЖДЁННУЮ связь с этим документом и
    пересчитывает предложения связей сущность↔сущность: подтверждение человеком —
    самый качественный сигнал в системе, и ради него весь путь и существует.

    Имя и тип берутся из тела запроса, а не из повторного разбора текста: между
    показом и нажатием документ мог измениться, и подтверждать надо ровно то, что
    человек видел.
    """
    actor = _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    name = str(body.get("name") or "").strip()
    entity_type = str(body.get("entity_type") or EntityType.OTHER.value).strip()
    if not user_id or not name:
        raise HTTPException(status_code=400, detail="user_id and name are required")
    try:
        parsed_type = EntityType(entity_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown entity_type: {entity_type}") from exc

    state = _services(request)
    knowledge = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not knowledge or knowledge.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Knowledge object not found")

    # Существующий узел переиспользуется: подтверждение одного и того же имени в двух
    # документах не должно плодить двойников, которые потом придётся сливать вручную.
    entity = state.storage.find_entity_by_name(user_id, name)
    created = False
    if not entity:
        entity = state.kg.create_entity(
            user_id,
            name,
            parsed_type,
            metadata={"origin": "human_review", "accepted_by": actor.user_id},
        )
        created = True

    link = state.kg.link_knowledge_to_entity(
        knowledge_id,
        str(entity["id"]),
        user_id,
        confidence=1.0,
        evidence={"method": "human_review", "accepted_by": actor.user_id},
        status="accepted",
        reviewed_by=actor.user_id,
    )
    _audit(
        request,
        "admin.entity_suggestion.accept",
        "entity",
        str(entity["id"]),
        after={
            "name": name,
            "entity_type": parsed_type.value,
            "knowledge_object_id": knowledge_id,
            "entity_created": created,
        },
    )
    relations = await asyncio.to_thread(state.kg.suggest_relations_for_knowledge, user_id, knowledge_id)
    return {
        "entity": entity,
        "entity_created": created,
        "link": link,
        "relation_candidates": relations,
    }


@router.patch("/entity-links/{link_id}")
async def review_knowledge_entity_link(link_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "")
    try:
        link = _services(request).storage.set_knowledge_entity_link_status(
            link_id,
            user_id,
            status,
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not link:
        raise HTTPException(status_code=404, detail="Knowledge/entity link not found")
    _audit(
        request,
        "admin.knowledge.entity_link.review",
        "knowledge_entity_link",
        link_id,
        after=link,
    )
    # Утверждение связи человеком — самый качественный сигнал в системе, и он был
    # единственным, который в граф не возвращался. Предложения связей считались
    # ровно один раз, при рождении объекта, по тем связям, что автомат успел принять
    # сам; всё, что подтвердил владелец при разборе, для поиска связей не
    # существовало никогда. Второго шанса не было предусмотрено.
    #
    # Пересчёт идёт off-loop и только на принятии: отклонение связи новых
    # предложений не рождает, а `store_relation_candidate` идемпотентен и не
    # переоткрывает то, что человек уже отверг.
    relations: list[dict[str, Any]] = []
    if status == "accepted":
        relations = await asyncio.to_thread(
            _services(request).kg.suggest_relations_for_knowledge,
            user_id,
            str(link.get("knowledge_object_id") or ""),
        )
    return {"link": link, "relation_candidates": relations}


@router.patch("/knowledge/{knowledge_id}")
async def update_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = str(body.get("user_id") or "")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, target)
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
        "promotion_score",
        "metadata_json",
    }
    updates = {key: body[key] for key in allowed if key in body}
    try:
        after = state.storage.update_knowledge_fields(knowledge_id, target, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Fields the operator changed, not the document they live in — a note
    # edited ten times would otherwise leave ten full copies in a table that
    # cannot be cleaned.
    _audit(
        request,
        "admin.knowledge.update",
        "knowledge_object",
        knowledge_id,
        before=_knowledge_fingerprint(before),
        after={"changed_fields": sorted(updates), **(_knowledge_fingerprint(after) or {})},
    )
    return {"item": after}


@router.post("/knowledge/{knowledge_id}/restore")
async def restore_knowledge_version(knowledge_id: str, request: Request) -> dict[str, Any]:
    """Вернуть объект к состоянию из снимка версии.

    Версии писались и показывались, а вернуться к ним было нечем: поиск по пакету на
    `restore|revert|rollback` находил только восстановление БАЗЫ из бэкапа. При этом
    редактор содержимого в админке — одна textarea с полным текстом документа, в
    среднем на 16.5 тысяч знаков, и первая же настоящая ошибка упиралась в тупик.

    Откат создаёт НОВУЮ версию, а не перематывает историю: откатившийся по ошибке
    может откатиться обратно.
    """
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = str(body.get("user_id") or "")
    try:
        version = int(str(body.get("version")))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="version must be an integer") from exc
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, target)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    actor = getattr(request.state, "actor", None)
    try:
        after = state.storage.restore_knowledge_version(
            knowledge_id, target, version, reviewed_by=getattr(actor, "user_id", None)
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.knowledge.restore",
        "knowledge_object",
        knowledge_id,
        before=_knowledge_fingerprint(before),
        after={"restored_from_version": version, **(_knowledge_fingerprint(after) or {})},
    )
    return {"item": after, "restored_from_version": version}


@router.delete("/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before or not state.storage.soft_delete_knowledge_object(knowledge_id, user_id):
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    after = state.storage.get_knowledge_object(knowledge_id, user_id)
    _audit(
        request,
        "admin.knowledge.delete",
        "knowledge_object",
        knowledge_id,
        before=_knowledge_fingerprint(before),
        after=_knowledge_fingerprint(after),
    )
    return {"status": "soft_deleted"}


@router.get("/data/purgeable")
async def list_purgeable_data(
    request: Request,
    user_id: str | None = None,
    older_than_days: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    _require(request, "admin.data.purge")
    _audit_cross_tenant_read(request, "admin.purge.read", user_id)
    state = _services(request)
    days = state.settings.purge_retention_days if older_than_days is None else max(0, int(older_than_days))
    items = state.storage.list_purgeable_knowledge(user_id, older_than_days=days, limit=limit)
    return {"items": items, "older_than_days": days, "count": len(items)}


@router.post("/knowledge/{knowledge_id}/purge")
async def purge_knowledge_endpoint(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.data.purge")
    _protect_owner_target(request, user_id)
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    try:
        report = purge_knowledge(state.storage, state.settings, state.memory_vault, knowledge_id, user_id)
    except ValueError as exc:
        # Not yet soft-deleted: purge requires the two-phase delete first.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # A FINGERPRINT, never the body. `audit_log` carries BEFORE UPDATE and BEFORE
    # DELETE triggers that `RAISE(ABORT)`, so anything written here cannot be
    # removed afterwards by any code or any SQL. Auditing the whole row meant
    # purge — the one operation whose entire purpose is to destroy every trace of
    # a Knowledge Object — durably wrote that object's full text into the one
    # table nothing can clean. The CLI path already did the right thing
    # (`before_json=None`); this one did not.
    _audit(
        request,
        "admin.knowledge.purge",
        "knowledge_object",
        knowledge_id,
        before=_knowledge_fingerprint(before),
        after=report,
    )
    return {"status": "purged", "report": report}
