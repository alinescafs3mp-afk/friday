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

router = APIRouter()


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    storage = _services(request).storage
    counts: dict[str, int] = {}
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
    for table, alive_only in (
        ("users", ""),
        ("raw_objects", " WHERE deleted_at IS NULL"),
        ("knowledge_objects", " WHERE deleted_at IS NULL"),
        ("inbox", ""),
        ("entities", " WHERE deleted_at IS NULL"),
        ("relations", " WHERE deleted_at IS NULL"),
        ("conversations", ""),
        ("messages", ""),
        ("feedback", ""),
        ("feedback_state", ""),
        ("relation_candidates", ""),
        ("knowledge_conflicts", ""),
    ):
        # ``table`` and ``alive_only`` come only from the fixed allowlist above.
        row = storage.execute(f"SELECT COUNT(*) AS count FROM {table}{alive_only}").fetchone()  # nosec B608
        counts[table] = int(row["count"] if row else 0)
    pending = storage.execute("SELECT COUNT(*) AS count FROM inbox WHERE status='pending'").fetchone()
    # Onboarding hints surface only while there is nothing to review yet, so an
    # empty install shows next steps instead of empty tables.
    bootstrap = (
        _services(request).kg.get_bootstrap_suggestions(request.state.actor.user_id)
        if counts["knowledge_objects"] == 0
        else []
    )
    return {
        "counts": counts,
        "pending_inbox": int(pending["count"] if pending else 0),
        "backups": storage.list_backups()[:5],
        "database": storage.diagnostics(),
        "bootstrap_suggestions": bootstrap,
    }


@router.get("/settings")
async def settings_info(request: Request) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    return _services(request).settings.public_dict()


@router.get("/diagnostics")
async def diagnostics(request: Request, check_llm: bool = False) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    state = _services(request)
    return collect_diagnostics(state.settings, state.storage, check_llm_port=check_llm)
