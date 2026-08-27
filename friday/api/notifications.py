"""HTTP routes for notifications.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from friday.api.deps import _request_json, _require_bridge
from friday.config import FridaySettings
from friday.file_delivery import attachment_content_disposition
from friday.organs import may_push_to, resolve_chat_id
from friday.organs.engineer.terminal_delivery import (
    TERMINAL_NOTIFICATION_KIND,
    TerminalDeliveryError,
    read_terminal_notification_artifact,
    terminal_notification_projection,
)
from friday.permissions import AuthorizationError

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _terminal_delivery_actor(state: Any, row: Mapping[str, Any]) -> Any:
    """Rebuild exact current authority for one owner-bound terminal carrier."""

    storage = state.storage
    settings = state.settings
    actor_id = str(row.get("user_id") or "")
    chat_id = str(row.get("chat_id") or "")
    user = storage.get_user(actor_id)
    try:
        linked_actor_id = storage.resolve_identity("telegram", chat_id)
    except ValueError as exc:
        raise TerminalDeliveryError("terminal_authorization_changed") from exc
    if (
        not actor_id
        or not chat_id
        or not isinstance(user, Mapping)
        or str(user.get("status") or "") != "active"
        or getattr(settings, "engineer_mode_enabled", False) is not True
        or getattr(settings, "engineer_command_enabled", False) is not True
        or linked_actor_id != actor_id
        or resolve_chat_id(storage, actor_id) != chat_id
        or not may_push_to(settings, storage, actor_id, chat_id)
    ):
        raise TerminalDeliveryError("terminal_authorization_changed")
    try:
        actor = state.auth_service.actor_for_user(
            actor_id,
            source="engineer-terminal-bridge",
            identity_id=chat_id,
        )
        if (
            not actor.is_owner
            or actor.own_id != actor_id
            or str(actor.identity_id or "") != chat_id
        ):
            raise TerminalDeliveryError("terminal_authorization_changed")
        for capability in ("engineer.use", "engineer.command.manage", "files.read"):
            state.auth_service.require(actor, capability)
    except (AuthorizationError, ValueError) as exc:
        raise TerminalDeliveryError("terminal_authorization_changed") from exc
    return actor


def _terminal_artifact_snapshot(state: Any, notification_id: str) -> Any:
    """Authorize and consume exact bytes under one SQLite writer snapshot."""

    storage = state.storage
    with storage.transaction() as conn:
        row = conn.execute(
            """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status
                  FROM outbound_notifications AS n
                 WHERE n.id=? AND n.kind=? AND n.status='pending'""",
            (str(notification_id), TERMINAL_NOTIFICATION_KIND),
        ).fetchone()
        if row is None:
            raise TerminalDeliveryError("terminal_notification_unavailable")
        current = dict(row)
        actor = _terminal_delivery_actor(state, current)
        return read_terminal_notification_artifact(
            storage,
            state.settings.files_dir,
            current,
            tenant_id=actor.user_id,
            actor_id=actor.own_id,
            max_bytes=state.settings.max_upload_bytes,
        )


@router.get("/pending", tags=["notifications"])
async def notifications_pending(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    _require_bridge(request)
    settings: FridaySettings = request.app.state.settings
    storage = request.app.state.storage
    items = []
    undeliverable: list[str] = []
    invalid_terminal: list[str] = []
    for row in storage.list_pending_notifications(limit=limit):
        # Defence in depth — но ТЕМ ЖЕ предикатом, что у органов, которые эту
        # строку поставили. Пока здесь стоял только статический список,
        # самозарегистрированный человек получал худший из возможных исходов:
        # орган ставил ему уведомление, а выдача очереди объявляла строку
        # недоставляемой и гасила её вместе с `dedup_key` — то есть тот же
        # материал больше не мог быть предложен НИКОГДА, и всё это молча.
        if not may_push_to(settings, storage, str(row.get("user_id") or ""), str(row.get("chat_id"))):
            undeliverable.append(str(row["id"]))
            continue
        if row.get("kind") == TERMINAL_NOTIFICATION_KIND:
            try:
                actor = _terminal_delivery_actor(request.app.state, row)
                projection = terminal_notification_projection(
                    storage,
                    row,
                    tenant_id=actor.user_id,
                    actor_id=actor.own_id,
                )
            except TerminalDeliveryError:
                invalid_terminal.append(str(row["id"]))
                continue
            item = {
                "id": row["id"],
                "chat_id": row["chat_id"],
                "kind": TERMINAL_NOTIFICATION_KIND,
                "dedup_key": row.get("dedup_key") or "",
                "caption": projection["caption"],
                "artifact": projection["artifact"],
            }
        else:
            item = {
                "id": row["id"],
                "chat_id": row["chat_id"],
                "body": row["body"],
                # Тип и ключ нужны мосту, чтобы решение можно было принять прямо в
                # уведомлении: заявка на подтверждение приходит с кнопками, иначе
                # человеку пришлось бы идти за ней в /approvals — а весь смысл
                # проактивного сообщения в том, что оно доходит само.
                "kind": row.get("kind") or "",
                "dedup_key": row.get("dedup_key") or "",
            }
        items.append(item)
    # Skipping was not enough: the row stayed pending with attempts=0 and no way
    # to ever leave the queue, and the queue is drained oldest-first with a limit
    # of 20 — twenty such rows and every later notification stops being delivered,
    # silently. Retiring them here keeps the queue moving.
    retired: list[str] = []
    if undeliverable:
        retire_verified = getattr(storage, "discard_notifications_verified", None)
        if callable(retire_verified):
            retired = retire_verified(undeliverable, reason="chat_not_allowed")
        else:
            storage.discard_notifications(undeliverable, reason="chat_not_allowed")
    if invalid_terminal:
        retire_verified = getattr(storage, "discard_notifications_verified", None)
        if callable(retire_verified):
            retired.extend(
                retire_verified(invalid_terminal, reason="terminal_authorization_changed")
            )
        else:
            storage.discard_notifications(
                invalid_terminal,
                reason="terminal_authorization_changed",
            )
    result: dict[str, Any] = {"items": items, "count": len(items)}
    if retired:
        # Exact terminal tombstones let a bridge bound local strict-delivery
        # fences after a chat is revoked between delivery and ACK. Old bridges
        # ignore this additive field.
        result["retired"] = retired
    return result


@router.get("/{notification_id}/artifact", tags=["notifications"])
async def notification_artifact(notification_id: str, request: Request) -> Response:
    _require_bridge(request)
    try:
        stored = await run_in_threadpool(
            _terminal_artifact_snapshot,
            request.app.state,
            notification_id,
        )
    except TerminalDeliveryError:
        raise HTTPException(status_code=404, detail="Файл не найден") from None
    return Response(
        content=stored.content,
        media_type=stored.mime_type,
        headers={
            "Content-Disposition": attachment_content_disposition(stored.filename),
            "Content-Length": str(len(stored.content)),
            "X-Friday-SHA256": hashlib.sha256(stored.content).hexdigest(),
        },
    )


@router.post("/ack", tags=["notifications"])
async def notifications_ack(request: Request) -> dict[str, Any]:
    _require_bridge(request)
    body = await _request_json(request)
    raw_sent = body.get("sent")
    raw_failed = body.get("failed")
    raw_uncertain = body.get("uncertain")
    uncertain = [str(x) for x in raw_uncertain] if isinstance(raw_uncertain, list) else []
    uncertain_set = set(uncertain)
    sent = [str(x) for x in raw_sent] if isinstance(raw_sent, list) else []
    sent = [value for value in sent if value not in uncertain_set]
    terminal = uncertain_set | set(sent)
    failed = [str(x) for x in raw_failed] if isinstance(raw_failed, list) else []
    failed = [value for value in failed if value not in terminal]
    acknowledge = getattr(request.app.state.storage, "acknowledge_notifications", None)
    if callable(acknowledge):
        state_ids = acknowledge(sent, failed, uncertain)
    elif isinstance(raw_uncertain, list):
        request.app.state.storage.mark_notifications(sent, failed, uncertain)
        state_ids = {}
    else:
        request.app.state.storage.mark_notifications(sent, failed)
        state_ids = {}
    result = {"sent": len(sent), "failed": len(failed)}
    if isinstance(raw_uncertain, list):
        result["uncertain"] = len(uncertain)
    if state_ids:
        # This is the proof-bearing surface. Numeric fields above retain their
        # historical "accepted request entries" meaning for ordinary bridges.
        result["state_ids"] = state_ids
    return result
