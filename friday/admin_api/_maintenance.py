"""Admin API: audit log, backups and exports.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    FileResponse,
    HTTPException,
    Path,
    Query,
    Request,
    _audit,
    _protect_owner_target,
    _request_json,
    _require,
    _safe_runtime_file,
    _services,
)
from friday.workers._blocking import run_blocking

router = APIRouter()


@router.get("/audit")
async def audit_log(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    before: str | None = Query(default=None),
) -> dict[str, Any]:
    _require(request, "admin.audit.read")
    storage = _services(request).storage
    # The client echoes back the `anchor` it got on page one, so every later page walks
    # the same snapshot. Without it this list shifts under its own reader: each page
    # turn writes an `admin.audit.read` row at the head of a newest-first listing.
    items = storage.list_audit_log(user_id, limit=limit, offset=offset, before=before)
    total = storage.count_audit_log(user_id, before=before)
    anchor = before or (str(items[0].get("created_at")) if items else None)
    # Reading the audit trail is itself an auditable event (who inspected
    # whose history); a plain INSERT, so there is no recursion.
    _audit(
        request,
        "admin.audit.read",
        "audit_log",
        user_id or "*",
        after={"limit": limit, "offset": offset, "returned": len(items)},
    )
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
        "anchor": anchor,
    }


@router.get("/backups")
async def list_backups(request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    items = _services(request).storage.list_backups()
    return {"items": items, "count": len(items)}


@router.post("/backups")
async def create_backup(request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    body = await _request_json(request)
    label = str(body.get("label") or "manual")[:80]
    result = _services(request).storage.create_backup(label=label)
    _audit(request, "admin.backup.create", "backup", result.get("database"), after=result)
    return {"backup": result}


@router.post("/backups/{filename}/verify")
async def verify_backup(filename: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    try:
        result = _services(request).storage.verify_backup(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Резервная копия не найдена") from exc
    _audit(request, "admin.backup.verify", "backup", filename, after=result)
    return {"verification": result}


@router.get("/backups/{filename}/download")
async def download_backup(filename: str, request: Request):
    _require(request, "admin.backup.manage")
    path = _safe_runtime_file(
        _services(request).settings.backups_dir,
        str(_services(request).settings.backups_dir / Path(filename).name),
    )
    # A backup contains every tenant's data — the highest-value egress event.
    _audit(request, "admin.backup.download", "backup", path.name)
    return FileResponse(path, filename=path.name, media_type="application/vnd.sqlite3")


@router.post("/exports")
async def create_export(request: Request) -> dict[str, Any]:
    """Архивная выгрузка аккаунта одним JSON.

    ⚠️ Это АРХИВ, а не путь переезда, и ответ говорит это прямо. Формат
    `jericho-user-export-v3` не читает ничто — ни Friday, ни что-либо ещё: строка
    встречается во всём коде дважды, там где пишется и в тесте. Импорта не
    существует, `jericho import` — про документы, а не про выгрузку.

    Замерено на архиве владельца: 150.8 МБ UTF-8 компактной сериализации (файл
    пишется с отступами, то есть ещё в полтора-два раза больше), пик памяти 759 МБ при
    3.9 ГБ доступных. В выгрузку НЕ входят оригиналы файлов (684 МБ) и вектора.

    Настоящих способа переехать два, и оба названы в ответе: копия SQLite вместе с
    каталогом файлов, либо `memory-vault` — 1539 заметок Markdown, которые читаются
    чем угодно без Friday.

    Работа ушла с event loop: она синхронная и на секунды подвешивала весь сервер —
    ни одного HTTP-запроса, ни одного сообщения в Telegram, пока строится словарь на
    сотню миллионов знаков.
    """
    _require(request, "admin.export")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or request.state.actor.user_id)
    _protect_owner_target(request, user_id)
    try:
        result = await run_blocking(_services(request).storage.export_user, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(request, "admin.export.create", "user", user_id, after=result)
    return {
        "export": result,
        # Честно о том, чем эта выгрузка НЕ является. Человек, который считает её
        # способом переехать, узнает правду в худший момент — когда Friday уже нет.
        "readable_by": "archival JSON; no importer exists in Friday or elsewhere",
        "to_move_your_data": [
            "скопируйте базу SQLite вместе с каталогом files/ — это полный перенос",
            "memory-vault/ — заметки Markdown, читаются чем угодно и без Friday",
        ],
        "not_included": ["оригиналы загруженных файлов", "векторы поиска"],
    }


@router.get("/exports/{filename}/download")
async def download_export(filename: str, request: Request):
    _require(request, "admin.export")
    path = _safe_runtime_file(
        _services(request).settings.exports_dir,
        str(_services(request).settings.exports_dir / Path(filename).name),
    )
    # Creation is audited separately; the download is the actual egress event.
    _audit(request, "admin.export.download", "export", path.name)
    return FileResponse(path, filename=path.name, media_type="application/json")
