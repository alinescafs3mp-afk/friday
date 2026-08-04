"""Request helpers shared by the HTTP routers.

Lifted out of ``server.py`` so a router module can use them without importing the
server back — which would be a cycle. Names keep their leading underscore: they are
internal to the API layer, and keeping them identical made the extraction a pure move
with no call-site churn in server.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from friday.permissions import ActorContext
from friday.storage.models import AuditEntry, new_id

__all__ = [
    "_audit",
    "_parse_json_bool",
    "_parse_json_float",
    "_request_json",
    "_require",
    "_require_bridge",
    "_json_load",
    "_safe_owned_file",
]


async def _request_json(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "json_body", None)
    if isinstance(cached, dict):
        return cached
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Тело запроса должно быть корректным JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON-тело должно быть объектом")
    request.state.json_body = body
    return body


def _parse_json_bool(value: Any, *, field: str, default: bool) -> bool:
    """Accept only real JSON booleans for externally visible control flags."""

    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field}: нужно логическое значение")
    return value


def _parse_json_float(
    value: Any,
    *,
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a finite bounded number and reject booleans/NaN/infinity."""

    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field}: нужно число")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field}: нужно число") from exc
    if not math.isfinite(parsed):
        raise HTTPException(status_code=400, detail=f"{field}: нужно конечное число")
    if not minimum <= parsed <= maximum:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: значение от {minimum:g} до {maximum:g}",
        )
    return parsed


def _require(request: Request, capability: str) -> ActorContext:
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


def _audit(
    request: Request,
    action: str,
    target_type: str,
    target_id: str | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    request.app.state.storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            # Кто ДЕЙСТВОВАЛ, а не в чьём архиве: в общем архиве `user_id` у всех
            # один, и запись об удалении знания отвечала бы «кто-то из нас».
            user_id=request.state.actor.own_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip_address=getattr(request.state, "client_ip", ""),
            request_id=getattr(request.state, "request_id", ""),
        )
    )


def _require_bridge(request: Request) -> ActorContext:
    # The outbound queue is drained only by the Telegram bridge — the sole holder of
    # the bridge secret. Bearer/loopback actors are refused.
    actor = request.state.actor
    if actor.source != "telegram-bridge":
        raise HTTPException(status_code=403, detail="Требуется аутентификация моста")
    return actor


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _safe_owned_file(root: Path, candidate: str) -> Path:
    """Файл внутри хранилища — по пути, записанному при приёме.

    Путь принимается и АБСОЛЮТНЫЙ, и относительный корню. Записывались абсолютные, и
    это ломало перенос: замерено на живой базе — у всех 1671 документа в метаданных
    лежат абсолютные пути (3342 штуки, ни одного относительного), укоренённые в
    прежнем каталоге. После переезда на другую машину, смены `FRIDAY_HOME` или даже
    имени пользователя сохранённый путь оказывается ВНЕ текущего хранилища, и каждый
    файл отдаёт 404 — неотличимый от «файла нет».

    Относительная ветка идёт первой: она и есть правильная форма, и на ней перенос
    работает. Абсолютная оставлена для уже записанных строк — их 3342, и заставлять
    человека править JSON в SQLite ради нашей же ошибки нельзя.
    """
    root = root.resolve()
    text = str(candidate or "")
    if not text:
        raise HTTPException(status_code=404, detail="Файл не найден")
    given = Path(text)
    path = (root / given).resolve() if not given.is_absolute() else given.resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return path
