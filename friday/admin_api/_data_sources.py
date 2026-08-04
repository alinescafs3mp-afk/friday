"""Внешние базы-источники: объявить, посмотреть, прочитать схему, забыть.

Строка подключения через эти маршруты НЕ проходит ни в одну сторону: объявляется
имя переменной окружения, а сама строка живёт в окружении процесса. Поэтому здесь
нельзя случайно записать пароль в базу, и нельзя случайно отдать его наружу — его
тут просто нет.

Отдельно отдаётся признак `secret_present`: объявить источник заранее законно, но
человек должен видеть, задана ли переменная, иначе первый же запрос упадёт
непонятной ошибкой.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    HTTPException,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
    asyncio,
)
from friday.data_sources import DataSource, SourceUnavailableError, describe_source

router = APIRouter()


def _visible(row: dict[str, Any]) -> dict[str, Any]:
    """То, что можно показать. Секрета здесь нет — есть имя переменной."""

    return {
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "dsn_env": str(row["dsn_env"]),
        "description": str(row["description"] or ""),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        # Объявить источник заранее законно, но молчать об этом нельзя.
        "secret_present": bool(os.environ.get(str(row["dsn_env"]))),
    }


@router.get("/data-sources")
async def list_data_sources(request: Request, user_id: str = "") -> dict[str, Any]:
    _require(request, "data.read")
    target = _target_user(request, user_id or None)
    # Какие чужие базы человек подключил — сведение о нём, а не только о базах.
    _audit_cross_tenant_read(request, "admin.data_sources.read", target)
    rows = _services(request).storage.list_data_sources(target)
    return {"user_id": target, "sources": [_visible(row) for row in rows]}


@router.post("/data-sources")
async def declare_data_source(request: Request) -> dict[str, Any]:
    """Объявить источник. Строку подключения сюда передавать НЕЛЬЗЯ и незачем."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    source = DataSource(
        name=str(body.get("name") or "").strip(),
        kind=str(body.get("kind") or "").strip(),
        dsn_env=str(body.get("dsn_env") or "").strip(),
        description=str(body.get("description") or "").strip()[:500],
    )
    try:
        source.validate()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    stored = _services(request).storage.register_data_source(
        target,
        name=source.name,
        kind=source.kind,
        dsn_env=source.dsn_env,
        description=source.description,
        created_by=target,
    )
    _audit(request, "admin.data_source.declare", "data_source", source.name, after=_visible(stored))
    return _visible(stored)


@router.delete("/data-sources/{name}")
async def forget_data_source(name: str, request: Request, user_id: str = "") -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    target = _target_user(request, user_id or None)
    _protect_owner_target(request, target)
    if not _services(request).storage.forget_data_source(target, name):
        raise HTTPException(status_code=404, detail="Такой источник не объявлен")
    _audit(request, "admin.data_source.forget", "data_source", name)
    return {"status": "forgotten", "name": name}


@router.get("/data-sources/{name}/schema")
async def data_source_schema(name: str, request: Request, user_id: str = "") -> dict[str, Any]:
    """Таблицы и столбцы источника — чтобы человек видел, что там вообще есть."""

    _require(request, "data.read")
    target = _target_user(request, user_id or None)
    _audit_cross_tenant_read(request, "admin.data_source.schema", target, source=name)
    row = _services(request).storage.get_data_source(target, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Такой источник не объявлен")
    source = DataSource(
        name=str(row["name"]),
        kind=str(row["kind"]),
        dsn_env=str(row["dsn_env"]),
        description=str(row["description"] or ""),
    )
    dsn = os.environ.get(source.dsn_env, "")
    if not dsn:
        raise HTTPException(
            status_code=409,
            detail=f"Переменная {source.dsn_env} не задана — подключаться нечем",
        )
    try:
        # Чужая база по сети: держать на ней event loop нельзя, соседние
        # разговоры ждут. Тот же приём, что у обхода графа.
        return await asyncio.to_thread(describe_source, source, dsn)
    except SourceUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
