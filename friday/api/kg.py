"""Knowledge-graph HTTP routes.

These routes were previously nested inside ``create_app``. The ``/api/kg`` prefix
lives on this router, while ``tests/test_route_inventory.py`` pins the HTTP surface
so later extraction work cannot silently add or lose an operation.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from friday.api.deps import _audit, _parse_json_float, _request_json, _require
from friday.http_errors import relation_history_http_detail
from friday.knowledge_graph import (
    _is_live_graph_entity,
    _public_relation,
    _safe_conflict_card,
    _safe_conflict_result,
    _safe_event_time,
    _safe_knowledge_card,
    _safe_merge_result,
    _validated_history_snapshot_status,
    normalize_event_date,
)
from friday.storage._graph import (
    _bounded_entity_by_id,
    _bounded_entity_listing_rows,
    _bounded_merge_history_rows,
    _bounded_relation_candidate_by_id,
    _bounded_relation_candidate_rows,
    _count_merge_history,
)
from friday.storage._knowledge import (
    _bounded_knowledge_conflict_by_id,
    _bounded_knowledge_conflict_rows,
)
from friday.storage._privacy import (
    _not_private_bounded_json_dependency,
    _not_private_entity_material_dependency,
)
from friday.storage.models import (
    EntityType,
    RelationHistorySnapshotError,
    RelationType,
    ResolutionStatus,
    normalize_known_at,
)
from friday.workers._blocking import run_blocking

router = APIRouter(prefix="/api/kg", tags=["knowledge-graph"])

_RESERVED_RELATION_PROVENANCE_KEYS = frozenset(
    {
        "origin",
        "source",
        "candidate_id",
        "reviewed_by",
        "reviewed_at",
        "created_by",
        "confidence",
        "evidence",
        "knowledge_object_id",
        "link_ids",
    }
)
_RELATION_AUDIT_FIELDS = (
    "id",
    "source_entity_id",
    "target_entity_id",
    "relation_type",
    "weight",
    "valid_from",
    "valid_to",
    "created_at",
)
_PUBLIC_ENTITY_VERSION_LIMIT = 100
_PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES = 1_048_576
_PUBLIC_ENTITY_LIST_LIMIT = 200
_PUBLIC_ENTITY_SEARCH_LIMIT = 25
_RELATION_CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


def _public_count(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), 1_000_000_000))
    except (TypeError, ValueError, OverflowError):
        return 0


def _public_entity_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded entity identity for public cards; internal metadata stays local."""

    aliases_value = raw.get("aliases")
    if not isinstance(aliases_value, list):
        try:
            decoded = json.loads(str(raw.get("aliases_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
        aliases_value = decoded if isinstance(decoded, list) else []
    aliases = [str(item)[:240] for item in aliases_value[:20]]
    card: dict[str, Any] = {
        "id": str(raw.get("id") or "")[:160],
        "name": str(raw.get("name") or "")[:240],
        "entity_type": str(raw.get("entity_type") or "")[:80],
        "aliases": aliases,
        # Telegram clients predating the public-card projection read this field.
        # It is the same bounded public alias list, not the stored raw JSON blob.
        "aliases_json": json.dumps(aliases, ensure_ascii=False),
        "description": str(raw.get("description") or "")[:500],
        "version": _public_count(raw.get("version")),
        "created_at": str(raw.get("created_at") or "")[:64],
        "updated_at": str(raw.get("updated_at") or "")[:64],
    }
    if "_match_score" in raw:
        try:
            card["_match_score"] = max(0.0, min(float(raw.get("_match_score") or 0.0), 1.0))
        except (TypeError, ValueError, OverflowError):
            card["_match_score"] = 0.0
    if "_match_method" in raw:
        card["_match_method"] = str(raw.get("_match_method") or "")[:80]
    for field in ("_relation_count", "_knowledge_count"):
        if field in raw:
            card[field] = _public_count(raw.get(field))
    return card


def _public_container_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Public container identity plus bounded hierarchy/count fields."""

    card = _public_entity_card(raw)
    parent_id = str(raw.get("parent_id") or "")[:160]
    card["parent_id"] = parent_id or None
    card["knowledge_count"] = _public_count(raw.get("knowledge_count"))
    return card


def _entity_audit_fingerprint(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Content-free mutation evidence safe for an append-only audit ledger."""

    public = _public_entity_card(raw)

    aliases_value = raw.get("aliases")
    if isinstance(aliases_value, list):
        aliases_text = json.dumps(aliases_value, ensure_ascii=False, separators=(",", ":"))
        alias_count = len(aliases_value)
    else:
        aliases_text = str(raw.get("aliases_json") or "[]")
        try:
            decoded_aliases = json.loads(aliases_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded_aliases = []
        alias_count = len(decoded_aliases) if isinstance(decoded_aliases, list) else 0
    metadata_value = raw.get("metadata")
    metadata_text = (
        json.dumps(metadata_value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(metadata_value, dict)
        else str(raw.get("metadata_json") or "{}")
    )
    return {
        "id": public["id"],
        "entity_type": public["entity_type"],
        "version": public["version"],
        "created_at": public["created_at"],
        "updated_at": public["updated_at"],
        "deleted": bool(raw.get("deleted_at")),
        "canonical": bool(raw.get("canonical", 1)),
        "merged": bool(raw.get("merged_into_id")),
        "name_chars": min(len(str(raw.get("name") or "")), 1_000_000_000),
        "description_chars": min(len(str(raw.get("description") or "")), 1_000_000_000),
        "aliases_chars": min(len(aliases_text), 1_000_000_000),
        "alias_count": min(alias_count, 1_000_000_000),
        "metadata_chars": min(len(metadata_text), 1_000_000_000),
    }


def _public_duplicate_scan_report(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Progress only; the cursor may contain private names or aliases."""

    output: dict[str, Any] = {}
    for field in (
        "entities",
        "pairs_examined",
        "keys_total",
        "keys_examined",
        "keys_pending",
        "candidates",
        "suggested",
        "pending_total",
        "sweeps",
    ):
        if field in raw:
            output[field] = _public_count(raw.get(field))
    for field in ("partial", "resumed", "complete"):
        if field in raw:
            output[field] = bool(raw.get(field))
    return output


def _public_merge_history_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or "")[:160],
        "source_entity_id": str(raw.get("source_entity_id") or "")[:160],
        "target_entity_id": str(raw.get("target_entity_id") or "")[:160],
        "created_at": str(raw.get("created_at") or "")[:64],
        "undone_at": str(raw.get("undone_at") or "")[:64],
        "undoable": bool(raw.get("undoable")),
        "transfer_bytes": _public_count(raw.get("transfer_bytes")),
        "links_moved_count": _public_count(raw.get("links_moved_count")),
        "links_suppressed_count": _public_count(raw.get("links_suppressed_count")),
        "relations_count": _public_count(raw.get("relations_count")),
        "candidates_closed_count": _public_count(raw.get("candidates_closed_count")),
    }


def _public_merge_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded mutation result shared by owner and admin merge routes."""

    return _safe_merge_result(raw)


def _public_relation_candidate_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded relation-review card without excerpts or reviewer identity."""

    limits = {
        "id": 160,
        "source_entity_id": 160,
        "target_entity_id": 160,
        "relation_type": 80,
        "status": 40,
        "created_at": 64,
        "reviewed_at": 64,
        "source_name": 240,
        "source_type": 80,
        "target_name": 240,
        "target_type": 80,
    }
    card: dict[str, Any] = {field: str(raw.get(field) or "")[:limit] for field, limit in limits.items()}
    try:
        confidence = float(raw.get("confidence") or 0.0)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    card["confidence"] = max(0.0, min(confidence, 1.0)) if math.isfinite(confidence) else 0.0
    raw_evidence = str(raw.get("evidence_json") or "")
    evidence_present = (
        bool(raw.get("evidence_present"))
        if "evidence_present" in raw
        else raw_evidence not in {"", "{}", "[]", "null"}
    )
    evidence_bytes = (
        raw.get("evidence_bytes")
        if "evidence_bytes" in raw
        else len(raw_evidence.encode("utf-8", errors="replace"))
    )
    card["evidence"] = {
        "present": evidence_present,
        "bytes": _public_count(evidence_bytes),
    }
    return card


def _relation_candidate_audit_fingerprint(raw: Mapping[str, Any]) -> dict[str, Any]:
    card = _public_relation_candidate_card(raw)
    return {
        field: card[field]
        for field in (
            "id",
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            "confidence",
            "status",
            "created_at",
            "reviewed_at",
            "evidence",
        )
    }


def _public_conflict_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _safe_conflict_card(raw)


def _public_conflict_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _safe_conflict_result(raw)


def _conflict_audit_fingerprint(raw: Mapping[str, Any]) -> dict[str, Any]:
    projected = _safe_conflict_result(raw)
    conflict = projected.get("conflict") if isinstance(projected.get("conflict"), Mapping) else projected
    safe: Mapping[str, Any] = conflict if isinstance(conflict, Mapping) else {}
    output: dict[str, Any] = {
        field: safe.get(field)
        for field in (
            "id",
            "knowledge_a_id",
            "knowledge_b_id",
            "conflict_type",
            "confidence",
            "status",
            "created_at",
            "reviewed_at",
            "evidence",
            "resolution_note_chars",
        )
    }
    for field in ("winner_id", "deprecated_id"):
        if field in projected:
            output[field] = str(projected.get(field) or "")[:160]
    return output


def _merge_audit_fingerprint(raw: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "merge_id": str(raw.get("merge_id") or raw.get("_merge_id") or "")[:160],
        "source_entity_id": str(raw.get("source_entity_id") or "")[:160],
        "target_entity_id": str(raw.get("target_entity_id") or "")[:160],
    }
    for field in ("merged_into", "source", "target"):
        entity = raw.get(field)
        if isinstance(entity, Mapping):
            output[field] = _entity_audit_fingerprint(entity)
    if raw.get("undone_at"):
        output["undone_at"] = str(raw.get("undone_at") or "")[:64]
    return output


def _bounded_public_entity_versions(
    storage: Any,
    user_id: str,
    entity_id: str,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Small version-card projection; raw snapshots may contain huge metadata."""

    safe_snapshot = (
        "CASE WHEN length(CAST(COALESCE(v.snapshot_json,'') AS BLOB))"
        f"<={_PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES} "
        "AND json_valid(v.snapshot_json) AND json_type(v.snapshot_json)='object' "
        "THEN v.snapshot_json ELSE '{}' END"
    )
    visible_snapshot = _not_private_bounded_json_dependency(
        "v.snapshot_json",
        "v.user_id",
        max_bytes=_PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES,
    )
    rows = storage.execute(
        f"""SELECT v.id, v.entity_id, v.version, v.created_at,
                  CASE WHEN json_valid({safe_snapshot})
                       THEN substr(COALESCE(json_extract({safe_snapshot}, '$.name'), ''), 1, 240)
                       ELSE '' END AS name,
                  CASE WHEN json_valid({safe_snapshot})
                       THEN substr(COALESCE(json_extract({safe_snapshot}, '$.entity_type'), ''), 1, 80)
                       ELSE '' END AS entity_type,
                  CASE WHEN json_valid({safe_snapshot})
                       THEN substr(COALESCE(json_extract({safe_snapshot}, '$.description'), ''), 1, 500)
                       ELSE '' END AS description
             FROM entity_versions v
             JOIN entities current_entity
               ON current_entity.id=v.entity_id AND current_entity.user_id=v.user_id
              AND {_not_private_entity_material_dependency("current_entity")}
            WHERE v.entity_id=? AND v.user_id=?
              AND {visible_snapshot}
              AND json_extract({safe_snapshot}, '$.id')=v.entity_id
              AND json_extract({safe_snapshot}, '$.user_id')=v.user_id
              AND (
                  json_extract({safe_snapshot}, '$.merged_into_id') IS NULL
                  OR EXISTS (
                      SELECT 1 FROM entities historical_merge_target
                       WHERE historical_merge_target.id=
                                 json_extract({safe_snapshot}, '$.merged_into_id')
                         AND historical_merge_target.user_id=v.user_id
                         AND {_not_private_entity_material_dependency("historical_merge_target")}
                  )
              )
            ORDER BY v.version DESC, v.id DESC
            LIMIT ?""",  # nosec B608
        (entity_id, user_id, _PUBLIC_ENTITY_VERSION_LIMIT + 1),
    ).fetchall()
    projected = [
        {
            "id": str(row["id"] or "")[:160],
            "entity_id": str(row["entity_id"] or "")[:160],
            "version": int(row["version"] or 0),
            "created_at": str(row["created_at"] or "")[:64],
            "snapshot": {
                "name": str(row["name"] or ""),
                "entity_type": str(row["entity_type"] or ""),
                "description": str(row["description"] or ""),
            },
        }
        for row in rows[:_PUBLIC_ENTITY_VERSION_LIMIT]
    ]
    return projected, len(rows), len(rows) > _PUBLIC_ENTITY_VERSION_LIMIT


def _relation_audit_after(relation: dict[str, Any]) -> dict[str, Any]:
    """A second, deliberately smaller allowlist for append-only audit storage."""

    return {field: relation[field] for field in _RELATION_AUDIT_FIELDS if field in relation}


def _normalize_graph_boundaries(as_of: str = "", known_at: str = "") -> tuple[str, str]:
    """Parse only caller-owned temporal input, before graph work can fail."""

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
    q: str | None = Query(None, max_length=500),
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
        bounded = min(limit, _PUBLIC_ENTITY_SEARCH_LIMIT)
        matches = await run_blocking(
            state.kg.search_entities,
            actor.user_id,
            q.strip(),
            limit=bounded + 1,
            entity_type=parsed_type,
        )
        items = [_public_entity_card(item) for item in matches[:bounded]]
        return {
            "items": items,
            "count": len(items),
            "matched_at_least": len(matches),
            "truncated": len(matches) > bounded,
        }
    bounded = min(limit, _PUBLIC_ENTITY_LIST_LIMIT)
    rows = await run_blocking(
        _bounded_entity_listing_rows,
        state.storage,
        actor.user_id,
        entity_types=(parsed_type.value,) if parsed_type is not None else (),
        limit=bounded + 1,
    )
    total = await run_blocking(state.storage.count_entities, actor.user_id, parsed_type)
    items = [_public_entity_card(item) for item in rows[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "matched_at_least": total,
        "truncated": total > len(items),
    }


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
    public_entity = _public_entity_card(entity)
    _audit(
        request,
        "entity.create",
        "entity",
        entity.get("id"),
        after=_entity_audit_fingerprint(entity),
    )
    return {"entity": public_entity}


@router.get("/containers", tags=["knowledge-graph"])
async def list_containers(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    raw_items = await run_blocking(request.app.state.kg.list_containers, actor.user_id)
    items = [_public_container_card(item) for item in raw_items]
    return {
        "items": items,
        "count": len(items),
        "matched_at_least": int(getattr(raw_items, "matched_at_least", len(raw_items))),
        "truncated": bool(getattr(raw_items, "truncated", False)),
    }


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
    public_container = _public_container_card(container)
    _audit(
        request,
        "container.create",
        "entity",
        container.get("id"),
        after=_entity_audit_fingerprint(container),
    )
    return {"container": public_container}


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
    return {"entity": _public_entity_card(entity), **profile}


@router.get("/entities/{entity_id}", tags=["knowledge-graph"])
async def get_entity(
    entity_id: str,
    request: Request,
    known_at: str = Query("", max_length=64),
) -> dict[str, Any]:
    """Сущность с её связями, документами и историей версий.

    Та же болезнь, что чинили у `/entity-profile`, жила и здесь: синхронные
    запросы в `async def` (то есть остановка event loop для ВСЕХ) и список
    документов, выбранный как `k.*` — с полными телами. Замерено на копии боевой
    базы: 8.66 МБ JSON на самой широкой сущности, 79 мс SQL плюс 61 мс сериализации,
    и всё это ради восьми заголовков, которые печатает вызывающий.

    `known_at` выбирает прежнюю проекцию ТОЛЬКО утверждённых relations. Имя
    сущности и карточки документов остаются текущими, что ответ называет через
    `identity_basis=current_names`, а не выдаёт за историческую идентичность.
    """
    actor = _require(request, "kg.read")
    state = request.app.state
    _, normalized_known_at = _normalize_graph_boundaries(known_at=known_at)
    history_status: dict[str, Any] = {}
    # Validate/floor-check before the entity lookup. A bad transaction snapshot
    # must not turn into an unrelated 404 merely because the requested entity is
    # absent, and the normalized boundary below is the only one relation storage
    # receives.
    if normalized_known_at:
        try:
            history_status = _validated_history_snapshot_status(
                await run_blocking(
                    state.storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
        except RelationHistorySnapshotError as exc:
            raise HTTPException(status_code=400, detail=relation_history_http_detail(exc)) from exc
    try:
        entity = await run_blocking(_bounded_entity_by_id, state.storage, entity_id, actor.user_id)
        if not _is_live_graph_entity(entity):
            if normalized_known_at:
                confirmed = _validated_history_snapshot_status(
                    await run_blocking(
                        state.storage.relation_history_status,
                        actor.user_id,
                        known_at=normalized_known_at,
                    ),
                    requested_known_at=normalized_known_at,
                )
                if confirmed != history_status:
                    raise RelationHistorySnapshotError(
                        "relation history status changed while checking the requested entity"
                    )
            raise HTTPException(status_code=404, detail="Сущность не найдена")
        assert entity is not None  # narrowed by the fail-closed 404 branch above
        relations = await run_blocking(
            state.kg.get_entity_relations,
            entity_id,
            actor.user_id,
            known_at=normalized_known_at,
        )
        knowledge = await run_blocking(
            state.storage.get_entity_knowledge_cards, actor.user_id, entity_id, limit=50
        )
        knowledge_total = await run_blocking(
            state.storage.count_entity_knowledge,
            actor.user_id,
            entity_id,
        )
        versions, versions_matched, versions_truncated = await run_blocking(
            _bounded_public_entity_versions,
            state.storage,
            actor.user_id,
            entity_id,
        )
        if normalized_known_at:
            confirmed = _validated_history_snapshot_status(
                await run_blocking(
                    state.storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while publishing the requested entity"
                )
            history_status = confirmed
    except RelationHistorySnapshotError as exc:
        raise HTTPException(status_code=400, detail=relation_history_http_detail(exc)) from exc
    result = {
        "entity": _public_entity_card(entity),
        "relations": relations,
        "relations_matched_at_least": int(getattr(relations, "matched_at_least", len(relations))),
        "relations_truncated": bool(getattr(relations, "truncated", False)),
        "knowledge": [_safe_knowledge_card(item) for item in knowledge],
        "knowledge_matched_at_least": knowledge_total,
        "knowledge_truncated": knowledge_total > len(knowledge),
        "versions": versions,
        "versions_matched_at_least": versions_matched,
        "versions_truncated": versions_truncated,
        "temporal_basis": "bitemporal" if normalized_known_at else "valid_time",
    }
    if history_status:
        result.update(history_status)
    return result


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
    public_after = _public_entity_card(after)
    audit_after = _entity_audit_fingerprint(after)
    audit_after["changed_fields"] = sorted(fields)
    _audit(
        request,
        "entity.update",
        "entity",
        entity_id,
        before=_entity_audit_fingerprint(before),
        after=audit_after,
    )
    return {"entity": public_after}


@router.delete("/entities/{entity_id}", tags=["knowledge-graph"])
async def delete_entity(entity_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    state = request.app.state
    before = state.kg.get_entity(entity_id, actor.user_id)
    if not before or not state.kg.delete_entity(actor.user_id, entity_id):
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    _audit(request, "entity.delete", "entity", entity_id, before=_entity_audit_fingerprint(before))
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
    public_entity = _public_entity_card(entity)
    _audit(
        request,
        "entity.undelete",
        "entity",
        entity_id,
        after=_entity_audit_fingerprint(entity),
    )
    return {"entity": public_entity}


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
    public_after = _public_entity_card(after)
    _audit(
        request,
        "entity.restore",
        "entity",
        entity_id,
        before=_entity_audit_fingerprint(before),
        after=_entity_audit_fingerprint(after),
    )
    return {"entity": public_after, "restored_from_version": version}


@router.post("/relations", tags=["knowledge-graph"])
async def create_relation(request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.write")
    body = await _request_json(request)
    try:
        raw_metadata = body.get("metadata")
        user_metadata: dict[str, Any] = (
            {
                str(key): value
                for key, value in raw_metadata.items()
                if str(key) not in _RESERVED_RELATION_PROVENANCE_KEYS
            }
            if isinstance(raw_metadata, dict)
            else {}
        )
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
    public_relation = _public_relation(relation.to_row())
    idempotent_replay = bool(getattr(relation, "_idempotent_replay", False))
    audit_after = _relation_audit_after(public_relation)
    if idempotent_replay:
        audit_after["idempotent_replay"] = True
    _audit(
        request,
        "relation.create.idempotent" if idempotent_replay else "relation.create",
        "relation",
        relation.id,
        after=audit_after,
    )
    return {"relation": public_relation, "idempotent_replay": idempotent_replay}


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
    known_at: str = Query(
        "",
        max_length=64,
        description="Что граф знал к offset-aware RFC3339 timestamp (transaction-time)",
    ),
) -> dict[str, Any]:
    """Окрестность узла по valid-time и, при запросе, transaction-time.

    Отменённая связь при заданной дате возвращается: «кончилось» — это не
    «не было», и различить их можно только спросив про конкретный день.
    `known_at` отдельно ограничивает, что системе уже было известно. Эти две
    оси намеренно не подменяют друг друга.
    """

    actor = _require(request, "kg.read")
    normalized_as_of, normalized_known_at = _normalize_graph_boundaries(as_of, known_at)
    try:
        graph = await run_blocking(
            request.app.state.kg.get_entity_graph,
            actor.user_id,
            entity_id,
            depth,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
        )
    except RelationHistorySnapshotError as exc:
        raise HTTPException(status_code=400, detail=relation_history_http_detail(exc)) from exc
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
    total = await run_blocking(
        request.app.state.storage.count_resolution_candidates,
        actor.user_id,
        ResolutionStatus.SUGGESTED,
    )
    return {
        "items": pending,
        "count": len(pending),
        "total": total,
        "matched_at_least": int(getattr(pending, "matched_at_least", len(pending))),
        "truncated": total > len(pending),
        "scan": _public_duplicate_scan_report(report),
    }


@router.get("/resolutions", tags=["knowledge-graph"])
async def list_resolutions(
    request: Request,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    try:
        parsed = ResolutionStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус объединения") from exc
    state = request.app.state
    items = await run_blocking(
        state.kg.resolver.get_resolutions,
        actor.user_id,
        parsed,
        limit=limit,
        offset=offset,
    )
    total = await run_blocking(state.storage.count_resolution_candidates, actor.user_id, parsed)
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
        "matched_at_least": int(getattr(items, "matched_at_least", len(items))),
        "truncated": offset + len(items) < total,
    }


@router.get("/resolutions/pending", tags=["knowledge-graph"])
async def pending_resolutions(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    # Enriched with both entities' names and link/relation counts so review
    # surfaces (e.g. the Telegram /merges flow) can show a human-readable pair.
    actor = _require(request, "kg.read")
    # Off the event loop: enrichment is six queries per candidate, and the whole
    # request measured 317 seconds at 5000 entities — synchronously, inside an
    # `async def`, so nothing else was served for its duration.
    items = await run_blocking(
        request.app.state.kg.resolver.get_pending_resolutions,
        actor.user_id,
        limit=limit,
    )
    total = await run_blocking(
        request.app.state.storage.count_resolution_candidates, actor.user_id, ResolutionStatus.SUGGESTED
    )
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "matched_at_least": int(getattr(items, "matched_at_least", len(items))),
        "truncated": total > len(items),
    }


@router.post("/resolutions/{candidate_id}/accept", tags=["knowledge-graph"])
async def accept_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "kg.merge")
    body = await _request_json(request)
    try:
        merged = await run_blocking(
            request.app.state.kg.resolver.accept_resolution,
            candidate_id,
            actor.user_id,
            target_entity_id=body.get("target_entity_id"),
            resolved_by=actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "entity.merge",
        "resolution",
        candidate_id,
        after=_merge_audit_fingerprint(merged),
    )
    return {"result": _public_merge_result(merged)}


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
    rows = await run_blocking(
        _bounded_merge_history_rows,
        request.app.state.storage,
        actor.user_id,
        limit=limit + 1,
    )
    total = await run_blocking(_count_merge_history, request.app.state.storage, actor.user_id)
    items = [_public_merge_history_card(row) for row in rows[:limit]]
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "matched_at_least": total,
        "truncated": total > len(items),
    }


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
    _audit(
        request,
        "entity.unmerge",
        "merge",
        merge_id,
        after=_merge_audit_fingerprint(result),
    )
    return {"result": _public_merge_result(result)}


@router.get("/relation-candidates", tags=["knowledge-graph"])
async def list_own_relation_candidates(
    request: Request,
    status: str | None = "suggested",
    limit: int = Query(5, ge=1, le=20),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> dict[str, Any]:
    """A bounded, content-free relation-review page for the current tenant."""

    actor = _require(request, "kg.read")
    storage = request.app.state.storage

    def _collect() -> tuple[list[dict[str, Any]], int]:
        rows = _bounded_relation_candidate_rows(
            storage,
            actor.user_id,
            status=status,
            limit=limit + 1,
            offset=offset,
        )
        total = storage.count_relation_candidates(actor.user_id, status=status)
        return rows, total

    try:
        rows, total = await run_blocking(_collect)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестный статус кандидата связи") from exc
    items = [_public_relation_candidate_card(item) for item in rows[:limit]]
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "status": status,
        "limit": limit,
        "offset": offset,
        "matched_at_least": total,
        "truncated": offset + len(items) < total,
    }


@router.post("/relation-candidates/{candidate_id}/review", tags=["knowledge-graph"])
async def review_own_relation_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    """Accept or reject one tenant-owned proposal without publishing its evidence body."""

    actor = _require(request, "kg.write")
    # The mutation response contains the two bounded entity cards. A custom
    # write-only grant must not turn into an accidental graph-read capability.
    request.app.state.auth_service.require(actor, "kg.read")
    if not _RELATION_CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise HTTPException(status_code=404, detail="Кандидат связи не найден")
    body = await _request_json(request)
    status = str(body.get("status") or "").casefold().strip()
    if status not in {"accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status должен быть accepted или rejected")

    storage = request.app.state.storage
    before = await run_blocking(
        _bounded_relation_candidate_by_id,
        storage,
        actor.user_id,
        candidate_id,
    )
    if before is None:
        raise HTTPException(status_code=404, detail="Кандидат связи не найден")
    current_status = str(before.get("status") or "suggested")
    if current_status in {"accepted", "rejected"} and current_status != status:
        raise HTTPException(status_code=409, detail="Кандидат связи уже решён другим образом")

    try:
        result = await run_blocking(
            request.app.state.kg.review_relation_candidate,
            actor.user_id,
            candidate_id,
            status,
            reviewed_by=actor.own_id,
        )
    except ValueError as exc:
        # A live candidate can race from suggested to the opposite terminal
        # state after the bounded preflight read. Dead endpoints return None at
        # storage authority and are deliberately indistinguishable from absence.
        raise HTTPException(
            status_code=409,
            detail="Кандидат связи уже решён другим образом",
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Кандидат связи не найден")

    public_result = _public_relation_candidate_card(result)
    _audit(
        request,
        f"relation_candidate.{status}",
        "relation_candidate",
        candidate_id,
        after=_relation_candidate_audit_fingerprint(result),
    )
    return {"status": status, "item": public_result}


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
        rows = await run_blocking(
            _bounded_knowledge_conflict_rows,
            storage,
            actor.user_id,
            status=status,
            limit=limit + 1,
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
        return [
            _public_conflict_card(attach_conflict_hint(storage, actor.user_id, item)) for item in rows[:limit]
        ]

    annotated = await run_blocking(_with_hints)
    return {
        "items": annotated,
        "count": len(annotated),
        "total": total,
        "status": status,
        "matched_at_least": total,
        "truncated": offset + len(annotated) < total,
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
    conflict = await run_blocking(
        _bounded_knowledge_conflict_by_id,
        kg.storage,
        actor.user_id,
        conflict_id,
    )
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
            public_result = _public_conflict_result(result)
            _audit(
                request,
                "knowledge_conflict.dismissed",
                "knowledge_conflict",
                conflict_id,
                after=_conflict_audit_fingerprint(result),
            )
            return {"status": "dismissed", "item": public_result}
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
    public_result = _public_conflict_result(result)
    _audit(
        request,
        "knowledge_conflict.resolved",
        "knowledge_conflict",
        conflict_id,
        after=_conflict_audit_fingerprint(result),
    )
    return {"status": "resolved", "winner_id": winner_id, "item": public_result}


@router.get("/timeline", tags=["knowledge-graph"])
async def event_timeline(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    actor = _require(request, "kg.read")
    try:
        page = await run_blocking(
            request.app.state.kg.timeline_page,
            actor.user_id,
            start=start,
            end=end,
            limit=limit,
            person_id=actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return page


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
    public_record = _safe_event_time(record) or {}
    _audit(request, "entity.time_set", "entity", entity_id, after=public_record)
    return {"event_time": public_record}
