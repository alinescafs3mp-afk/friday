"""Admin API: the console landing page: totals, settings and diagnostics.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    Request,
    _require,
    _services,
    collect_diagnostics,
)
from friday.storage._privacy import (
    _not_private_entity_material_dependency,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
    _not_private_relation_candidate_dependency,
    _not_private_relation_dependency,
)
from friday.workers._blocking import run_blocking

router = APIRouter()


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    return await run_blocking(_overview_sync, request)


def _overview_sync(request: Request) -> dict[str, Any]:
    """Build every SQLite-backed aggregate outside the serving event loop."""

    actor = _require(request, "admin.diagnostics")
    storage = _services(request).storage
    tenant_user_id = actor.user_id
    personal_user_id = actor.own_id

    def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = storage.execute(sql, params).fetchone()
        return int(row["count"] if row else 0)

    # Плитки считают ЖИВОЕ, а не строки в таблице.
    #
    # Удаление здесь мягкое: строка остаётся с проставленным `deleted_at`, и
    # голый COUNT показывал её вместе с остальными. Замерено на живой базе:
    # плитка «Знаний 1562» стояла рядом со страницей знаний, где их 1536, — и
    # объяснить разницу человеку было нечем, кроме «где-то что-то не сходится».
    # Доверие к обзору при этом теряется целиком: если врёт одно число, читать
    # остальные незачем.
    #
    # Предикат назван у каждой таблицы отдельно, а не выведен по имени столбца:
    # `deleted_at` есть ровно у четырёх из двенадцати (проверено PRAGMA на живой
    # базе), и молчаливое «добавим, если есть» скрыло бы появление пятой.
    raw_public = _not_private_raw_dependency("overview_raw")
    knowledge_public = _not_private_knowledge_dependency("overview_knowledge")
    inbox_public = _not_private_inbox_dependency("overview_inbox")
    entity_public = _not_private_entity_material_dependency("overview_entity")
    relation_public = _not_private_relation_dependency("overview_relation")
    relation_source_public = _not_private_entity_material_dependency("overview_relation_source")
    relation_target_public = _not_private_entity_material_dependency("overview_relation_target")
    candidate_public = _not_private_relation_candidate_dependency("overview_candidate")
    candidate_source_public = _not_private_entity_material_dependency("overview_candidate_source")
    candidate_target_public = _not_private_entity_material_dependency("overview_candidate_target")
    conflict_a_public = _not_private_knowledge_dependency("overview_conflict_a")
    conflict_b_public = _not_private_knowledge_dependency("overview_conflict_b")
    feedback_history = storage.get_feedback_stats(personal_user_id)
    counts: dict[str, int] = {
        "users": count("SELECT COUNT(*) AS count FROM users") if actor.is_owner else 1,
        "raw_objects": count(
            f"""SELECT COUNT(*) AS count FROM raw_objects overview_raw
                 WHERE overview_raw.user_id=? AND overview_raw.deleted_at IS NULL
                   AND {raw_public}""",  # nosec B608 - code-owned predicate
            (tenant_user_id,),
        ),
        "knowledge_objects": count(
            f"""SELECT COUNT(*) AS count FROM knowledge_objects overview_knowledge
                 WHERE overview_knowledge.user_id=? AND overview_knowledge.deleted_at IS NULL
                   AND {knowledge_public}""",  # nosec B608 - code-owned predicate
            (tenant_user_id,),
        ),
        "inbox": count(
            f"""SELECT COUNT(*) AS count FROM inbox overview_inbox
                 WHERE overview_inbox.user_id=? AND {inbox_public}""",  # nosec B608
            (tenant_user_id,),
        ),
        "entities": count(
            f"""SELECT COUNT(*) AS count FROM entities overview_entity
                 WHERE overview_entity.user_id=? AND overview_entity.deleted_at IS NULL
                   AND {entity_public}""",  # nosec B608
            (tenant_user_id,),
        ),
        "relations": count(
            f"""SELECT COUNT(*) AS count FROM relations overview_relation
                 JOIN entities overview_relation_source
                   ON overview_relation_source.id=overview_relation.source_entity_id
                  AND overview_relation_source.user_id=overview_relation.user_id
                  AND {relation_source_public}
                 JOIN entities overview_relation_target
                   ON overview_relation_target.id=overview_relation.target_entity_id
                  AND overview_relation_target.user_id=overview_relation.user_id
                  AND {relation_target_public}
                 WHERE overview_relation.user_id=? AND overview_relation.deleted_at IS NULL
                   AND {relation_public}""",  # nosec B608
            (tenant_user_id,),
        ),
        "conversations": count(
            "SELECT COUNT(*) AS count FROM conversations WHERE user_id=?",
            (personal_user_id,),
        ),
        "messages": count(
            "SELECT COUNT(*) AS count FROM messages WHERE user_id=?",
            (personal_user_id,),
        ),
        "feedback": sum(int(bucket.get("count") or 0) for bucket in feedback_history.values()),
        "feedback_state": storage.count_feedback_state(personal_user_id),
        "relation_candidates": count(
            f"""SELECT COUNT(*) AS count FROM relation_candidates overview_candidate
                 JOIN entities overview_candidate_source
                   ON overview_candidate_source.id=overview_candidate.source_entity_id
                  AND overview_candidate_source.user_id=overview_candidate.user_id
                  AND {candidate_source_public}
                 JOIN entities overview_candidate_target
                   ON overview_candidate_target.id=overview_candidate.target_entity_id
                  AND overview_candidate_target.user_id=overview_candidate.user_id
                  AND {candidate_target_public}
                 WHERE overview_candidate.user_id=? AND {candidate_public}""",  # nosec B608
            (tenant_user_id,),
        ),
        "knowledge_conflicts": count(
            f"""SELECT COUNT(*) AS count FROM knowledge_conflicts overview_conflict
                 JOIN knowledge_objects overview_conflict_a
                   ON overview_conflict_a.id=overview_conflict.knowledge_a_id
                  AND overview_conflict_a.user_id=overview_conflict.user_id
                  AND overview_conflict_a.deleted_at IS NULL AND {conflict_a_public}
                 JOIN knowledge_objects overview_conflict_b
                   ON overview_conflict_b.id=overview_conflict.knowledge_b_id
                  AND overview_conflict_b.user_id=overview_conflict.user_id
                  AND overview_conflict_b.deleted_at IS NULL AND {conflict_b_public}
                 WHERE overview_conflict.user_id=?""",  # nosec B608
            (tenant_user_id,),
        ),
    }
    pending_count = count(
        f"""SELECT COUNT(*) AS count FROM inbox overview_inbox
             WHERE overview_inbox.user_id=? AND overview_inbox.status='pending'
               AND {inbox_public}""",  # nosec B608
        (tenant_user_id,),
    )
    # Onboarding hints surface only while there is nothing to review yet, so an
    # empty install shows next steps instead of empty tables.
    bootstrap = (
        _services(request).kg.get_bootstrap_suggestions(tenant_user_id)
        if counts["knowledge_objects"] == 0
        else []
    )
    schema_row = storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    try:
        schema_version: int | None = int(schema_row["value"]) if schema_row else None
    except (TypeError, ValueError):
        schema_version = None
    fts_available = storage.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts' LIMIT 1"
    ).fetchone()
    database_path = storage.settings.database_path
    # The landing page used to call ``storage.diagnostics()`` here.  That is a
    # full operator audit: integrity_check over the whole database plus decoding
    # and privacy-projecting every historical knowledge snapshot.  A page view
    # must not impersonate that check or make the serving loop wait for it.  This
    # projection reports only facts which are cheap and current; the UI names the
    # integrity verdict as not run and sends the operator to Diagnostics for it.
    database = {
        "database_size_bytes": database_path.stat().st_size if database_path.exists() else 0,
        "schema_version": schema_version,
        "fts_available": bool(fts_available),
        "integrity_check": "not_run",
        "ok": None,
    }
    return {
        "counts": counts,
        "pending_inbox": pending_count,
        "backups": storage.list_backups(limit=5),
        "database": database,
        "bootstrap_suggestions": bootstrap,
    }


@router.get("/settings")
async def settings_info(request: Request) -> dict[str, Any]:
    return await run_blocking(_settings_info_sync, request)


def _settings_info_sync(request: Request) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    return _services(request).settings.public_dict()


@router.get("/diagnostics")
async def diagnostics(request: Request, check_llm: bool = False) -> dict[str, Any]:
    return await run_blocking(_diagnostics_sync, request, check_llm)


def _diagnostics_sync(request: Request, check_llm: bool) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    state = _services(request)
    return collect_diagnostics(
        state.settings,
        state.storage,
        check_llm_port=check_llm,
    )
