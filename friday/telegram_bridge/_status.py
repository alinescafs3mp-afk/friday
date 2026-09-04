"""One durable, monotonically edited Telegram status message per operation."""

from __future__ import annotations

import asyncio
import re
import weakref
from enum import Enum
from typing import Any

import httpx

from friday.orchestration.operation_progress import (
    MAX_REVISION,
    OperationMode,
    OperationProgressProjection,
    OperationStepState,
    ResultDeliveryState,
    build_operation_progress,
    render_operation_progress,
)
from friday.telegram_bridge._base import LOGGER, TELEGRAM_TEXT_LIMIT, split_for_telegram
from friday.telegram_bridge._engineer_progress import build_engineer_operation_progress

_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_MAX_WAIT_SEC = 30.0
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_status_sleep = asyncio.sleep


class TelegramStatusStage(str, Enum):
    """Closed, bridge-observable stages; never model-authored prose."""

    RECEIVING_MEDIA = "receiving_media"
    STAGING_DOCUMENTS = "staging_documents"
    BACKEND_WAIT = "backend_wait"
    DELIVERING_RESULT = "delivering_result"
    COMPLETE = "complete"
    STOPPED = "stopped"


def _elapsed_label(elapsed_sec: float) -> str:
    seconds = max(0, int(elapsed_sec))
    minutes, remainder = divmod(seconds, 60)
    if not minutes:
        return f"{remainder} с"
    if not remainder:
        return f"{minutes} мин"
    return f"{minutes} мин {remainder} с"


def _bytes_label(value: int) -> str:
    amount = max(0, int(value))
    if amount < 1024:
        return f"{amount} Б"
    if amount < 1024 * 1024:
        return f"{amount / 1024:.1f} КиБ"
    return f"{amount / (1024 * 1024):.1f} МиБ"


_CHAT_MEDIA_STEPS = (
    ("receiving_media", "получаю вложения из Telegram", "files"),
    ("staging_documents", "передаю вложения в ядро", "files"),
)
_CHAT_CORE_STEPS = (
    ("backend_wait", "ядро обрабатывает запрос", "none"),
    ("delivering_result", "отправляю готовый результат", "none"),
)


def _chat_step_state(
    step_id: str,
    stage: TelegramStatusStage,
    *,
    item_total: int,
    received_items: int,
    staged_items: int,
) -> OperationStepState:
    if stage is TelegramStatusStage.COMPLETE:
        return OperationStepState.COMPLETED
    order: list[str] = []
    if item_total > 0:
        order.extend(item[0] for item in _CHAT_MEDIA_STEPS)
    order.extend(item[0] for item in _CHAT_CORE_STEPS)
    current = stage.value if stage is not TelegramStatusStage.STOPPED else None
    if current not in order:
        current = "backend_wait" if item_total <= 0 else "receiving_media"
        if stage is TelegramStatusStage.STOPPED:
            if item_total > 0 and staged_items >= item_total:
                current = "backend_wait"
            elif item_total > 0 and received_items >= item_total:
                current = "staging_documents"
            elif item_total > 0:
                current = "receiving_media"
            else:
                current = "backend_wait"
    current_index = order.index(current)
    step_index = order.index(step_id)
    if stage is TelegramStatusStage.STOPPED:
        if step_index < current_index:
            return OperationStepState.COMPLETED
        return OperationStepState.CANCELLED
    if step_index < current_index:
        return OperationStepState.COMPLETED
    if step_index == current_index:
        return OperationStepState.RUNNING
    return OperationStepState.PENDING


def _chat_step_payload(
    step_id: str,
    label: str,
    evidence_class: str,
    state: OperationStepState,
    *,
    item_total: int,
    received_items: int,
    staged_items: int,
) -> dict[str, Any]:
    completed: int | None = None
    total: int | None = None
    percentage: int | None = None
    if evidence_class == "files" and item_total > 0:
        total = item_total
        if step_id == "receiving_media":
            completed = min(max(0, received_items), item_total)
        else:
            completed = min(max(0, staged_items), item_total)
        if state is OperationStepState.COMPLETED:
            completed = item_total
            percentage = 100
        elif state is OperationStepState.PENDING:
            completed = 0
            percentage = 0
        elif state is OperationStepState.RUNNING:
            percentage = None
        else:
            percentage = None
    elif state is OperationStepState.COMPLETED:
        percentage = 100
    elif state is OperationStepState.PENDING:
        percentage = 0
    return {
        "step_id": step_id,
        "safe_label": label,
        "state": str(state),
        "completed_units": completed,
        "total_units": total,
        "percentage": percentage,
        "evidence_class": evidence_class,
    }


def build_chat_operation_progress(
    stage: TelegramStatusStage,
    elapsed_sec: float,
    *,
    item_total: int = 0,
    received_items: int = 0,
    received_bytes: int = 0,
    staged_items: int = 0,
    staged_bytes: int = 0,
    operation_id: str = "chat:status",
    authenticated_turn_id: str = "chat:status",
    revision: int = 1,
) -> OperationProgressProjection:
    """Admit one chat-status projection from bridge-observable facts only."""

    del received_bytes, staged_bytes
    total = max(0, int(item_total))
    received = max(0, int(received_items))
    staged = max(0, int(staged_items))
    catalog = tuple(_CHAT_CORE_STEPS) if total <= 0 else _CHAT_MEDIA_STEPS + _CHAT_CORE_STEPS
    steps = [
        _chat_step_payload(
            step_id,
            label,
            evidence_class,
            _chat_step_state(
                step_id,
                stage,
                item_total=total,
                received_items=received,
                staged_items=staged,
            ),
            item_total=total,
            received_items=received,
            staged_items=staged,
        )
        for step_id, label, evidence_class in catalog
    ]
    terminal = stage in {TelegramStatusStage.COMPLETE, TelegramStatusStage.STOPPED}
    running = [item["step_id"] for item in steps if item["state"] == str(OperationStepState.RUNNING)]
    cancelled = [item["step_id"] for item in steps if item["state"] == str(OperationStepState.CANCELLED)]
    if running:
        active = running[0]
    elif cancelled:
        active = cancelled[0]
    else:
        active = steps[-1]["step_id"]
    if terminal:
        delivery = (
            ResultDeliveryState.CONFIRMED
            if stage is TelegramStatusStage.COMPLETE
            else ResultDeliveryState.UNCERTAIN
        )
    else:
        delivery = (
            ResultDeliveryState.IN_FLIGHT
            if stage is TelegramStatusStage.DELIVERING_RESULT
            else ResultDeliveryState.NOT_STARTED
        )
    return build_operation_progress(
        {
            "operation_id": operation_id,
            "authenticated_turn_id": authenticated_turn_id,
            "revision": max(1, int(revision)),
            "terminal": terminal,
            "mode": str(OperationMode.CHAT),
            "title": "Выполняю задачу",
            "ordered_steps": steps,
            "active_step_id": active,
            "elapsed_sec": max(0, int(elapsed_sec)),
            "hard_deadline_remaining_sec": None,
            "result_delivery_state": str(delivery),
            "plan_generation": 1,
        }
    )


def render_chat_status(
    stage: TelegramStatusStage,
    elapsed_sec: float,
    *,
    item_total: int = 0,
    received_items: int = 0,
    received_bytes: int = 0,
    staged_items: int = 0,
    staged_bytes: int = 0,
    operation_id: str = "chat:status",
    authenticated_turn_id: str = "chat:status",
    revision: int = 1,
) -> str:
    """Render chat status through the shared operation-progress contract."""

    return render_operation_progress(
        build_chat_operation_progress(
            stage,
            elapsed_sec,
            item_total=item_total,
            received_items=received_items,
            received_bytes=received_bytes,
            staged_items=staged_items,
            staged_bytes=staged_bytes,
            operation_id=operation_id,
            authenticated_turn_id=authenticated_turn_id,
            revision=revision,
        )
    )


def render_engineer_status(update: dict[str, Any]) -> str:
    """Render Engineer status through the shared operation-progress contract."""

    raw_revision = update.get("revision", 1)
    revision = (
        min(raw_revision, MAX_REVISION)
        if isinstance(raw_revision, int) and not isinstance(raw_revision, bool) and raw_revision >= 1
        else 1
    )
    return render_operation_progress(build_engineer_operation_progress(update, revision=revision))


def _validated_operation_id(value: object) -> str:
    operation_id = value if isinstance(value, str) else ""
    if _OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid Telegram status operation id")
    return operation_id


def _validated_sqlite_integer(value: object, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid Telegram status integer")
    if abs(value) > _MAX_SQLITE_INTEGER or (positive and value <= 0) or (not positive and value == 0):
        raise ValueError("invalid Telegram status integer")
    return value


def _validated_text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("invalid Telegram status text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError("invalid Telegram status text")
    if split_for_telegram(value, limit=TELEGRAM_TEXT_LIMIT) != [value]:
        raise ValueError("Telegram status text must fit one message")
    return value


class TelegramStatusMessageManager:
    """Persist and edit one Telegram message without exposing content to SQLite.

    Revisions are caller-owned monotonic integers. The durable terminal bit is
    absorbing: after it is stored, neither a stale nor a larger running update
    may change the user's final status.
    """

    def __init__(self, inbox: Any, *, api_url: str) -> None:
        self._inbox = inbox
        self._api_url = str(api_url).rstrip("/")
        self._locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def snapshot(self, chat_id: int, operation_id: str) -> dict[str, Any] | None:
        chat = _validated_sqlite_integer(chat_id, positive=False)
        operation = _validated_operation_id(operation_id)
        stored = self._inbox.telegram_status_message(chat, operation)
        if stored is not None:
            return stored
        fence = self._inbox.telegram_status_send_fence(chat, operation)
        if fence is None:
            return None
        return {
            "revision": int(fence["revision"]),
            "terminal": False,
            "ambiguous": True,
        }

    async def publish(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        operation_id: str,
        revision: int,
        text: str,
        *,
        terminal: bool = False,
        reply_to_message_id: int | None = None,
        create: bool = True,
    ) -> str:
        """Send once, then edit monotonically; replace if Telegram rejects edit."""

        chat = _validated_sqlite_integer(chat_id, positive=False)
        operation = _validated_operation_id(operation_id)
        current_revision = _validated_sqlite_integer(revision, positive=True)
        status_text = _validated_text(text)
        reply_id = (
            _validated_sqlite_integer(reply_to_message_id, positive=True)
            if reply_to_message_id is not None
            else None
        )
        key = (chat, operation)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            stored = self._inbox.telegram_status_message(chat, operation)
            fence = self._inbox.telegram_status_send_fence(chat, operation)
            if fence is not None:
                fence_revision = int(fence["revision"])
                if stored is not None and int(stored["revision"]) >= fence_revision:
                    # The accepted message coordinate won the crash race; this
                    # is the only proof that permits removing an old send fence.
                    self._inbox.clear_telegram_status_send_fence(
                        chat,
                        operation,
                        fence_revision,
                    )
                else:
                    # Telegram may have accepted a send whose response was lost.
                    # Without its message_id neither retry nor replacement can
                    # be made idempotent, so the ambiguity is absorbing.
                    return "uncertain"
            if stored is not None:
                stored_revision = int(stored["revision"])
                if bool(stored["terminal"]):
                    return "terminal"
                if current_revision <= stored_revision:
                    return "stale"
                message_id = int(stored["message_id"])
                try:
                    response = await self._post(
                        client,
                        "editMessageText",
                        {
                            "chat_id": chat,
                            "message_id": message_id,
                            "text": status_text,
                            "disable_web_page_preview": True,
                        },
                    )
                    if not self._is_not_modified(response):
                        response.raise_for_status()
                except asyncio.CancelledError:
                    raise
                except httpx.TransportError as exc:
                    # A replacement after either a pre-accept transport failure
                    # or an ambiguous one would leave the known status beside a
                    # duplicate. The same idempotent edit remains retryable.
                    LOGGER.debug("Telegram status edit outcome uncertain (%s)", type(exc).__name__)
                    raise
                except httpx.HTTPStatusError as exc:
                    if not self._is_proven_rejection(exc.response):
                        LOGGER.debug(
                            "Telegram status edit outcome uncertain (%s)",
                            type(exc).__name__,
                        )
                        raise
                    # Only a structurally valid Telegram 4xx proves that the edit
                    # was rejected and makes a replacement safe to attempt.
                    LOGGER.debug("Telegram status edit was rejected; replacing")
                    send_outcome, replacement_id = await self._fenced_send(
                        client,
                        chat,
                        operation,
                        current_revision,
                        status_text,
                        reply_to_message_id=reply_id,
                    )
                    if send_outcome == "uncertain":
                        return "uncertain"
                    if replacement_id is None:
                        raise RuntimeError("accepted Telegram status send has no message id") from exc
                    message_id = replacement_id
                    outcome = "replaced"
                else:
                    outcome = "edited"
                if not self._inbox.record_telegram_status_message(
                    chat,
                    operation,
                    message_id,
                    current_revision,
                    bool(terminal),
                    expected_revision=stored_revision,
                ):
                    outcome_after_miss = self._outcome_after_cas_miss(
                        chat,
                        operation,
                        current_revision,
                    )
                    self._inbox.clear_telegram_status_send_fence(
                        chat,
                        operation,
                        current_revision,
                    )
                    return outcome_after_miss
                self._inbox.clear_telegram_status_send_fence(
                    chat,
                    operation,
                    current_revision,
                )
                return outcome

            if not create:
                return "missing"
            send_outcome, initial_message_id = await self._fenced_send(
                client,
                chat,
                operation,
                current_revision,
                status_text,
                reply_to_message_id=reply_id,
            )
            if send_outcome == "uncertain" or initial_message_id is None:
                return "uncertain"
            message_id = initial_message_id
            if not self._inbox.record_telegram_status_message(
                chat,
                operation,
                message_id,
                current_revision,
                bool(terminal),
                expected_revision=None,
            ):
                outcome_after_miss = self._outcome_after_cas_miss(
                    chat,
                    operation,
                    current_revision,
                )
                self._inbox.clear_telegram_status_send_fence(
                    chat,
                    operation,
                    current_revision,
                )
                return outcome_after_miss
            self._inbox.clear_telegram_status_send_fence(
                chat,
                operation,
                current_revision,
            )
            return "sent"

    def _outcome_after_cas_miss(self, chat_id: int, operation_id: str, revision: int) -> str:
        stored = self._inbox.telegram_status_message(chat_id, operation_id)
        if stored is not None and bool(stored["terminal"]):
            return "terminal"
        if stored is not None and int(stored["revision"]) >= revision:
            return "stale"
        raise RuntimeError("Telegram status revision could not be persisted")

    async def _send(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        response = await self._post(client, "sendMessage", payload)
        response.raise_for_status()
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return _validated_sqlite_integer(message_id, positive=True)

    async def _fenced_send(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        operation_id: str,
        revision: int,
        text: str,
        *,
        reply_to_message_id: int | None,
    ) -> tuple[str, int | None]:
        """Send only as creator of a durable ambiguity fence."""

        state = self._inbox.begin_telegram_status_send(chat_id, operation_id, revision)
        if state != "armed":
            return "uncertain", None
        try:
            message_id = await self._send(
                client,
                chat_id,
                text,
                reply_to_message_id=reply_to_message_id,
            )
        except asyncio.CancelledError:
            # Cancellation can race a completed Telegram write. Preserve the
            # fence for the next process rather than guessing non-acceptance.
            raise
        except Exception as exc:
            if self._send_was_proven_rejected(exc) and self._inbox.clear_telegram_status_send_fence(
                chat_id,
                operation_id,
                revision,
            ):
                raise
            # URLs contain the bot token. Log only the exception class and turn
            # the uncertain result into an absorbing, content-free local state.
            LOGGER.debug("Telegram status send outcome uncertain (%s)", type(exc).__name__)
            return "uncertain", None
        return "accepted", message_id

    async def _post(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            response = await client.post(f"{self._api_url}/{method}", json=payload)
            if response.status_code != 429 or attempt >= _RATE_LIMIT_RETRIES:
                return response
            try:
                body = response.json()
                parameters = body.get("parameters") if isinstance(body, dict) else None
                retry_value = parameters.get("retry_after") if isinstance(parameters, dict) else 0.0
                retry_after = float(str(retry_value or "0"))
            except (TypeError, ValueError):
                retry_after = 0.0
            if retry_after <= 0.0:
                return response
            await _status_sleep(min(retry_after, _RATE_LIMIT_MAX_WAIT_SEC))
        raise RuntimeError("unreachable Telegram status retry state")

    @staticmethod
    def _is_not_modified(response: httpx.Response) -> bool:
        if response.status_code != 400:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        description = body.get("description") if isinstance(body, dict) else None
        return isinstance(description, str) and "message is not modified" in description.casefold()

    @staticmethod
    def _is_proven_rejection(response: httpx.Response) -> bool:
        if not 400 <= int(response.status_code) < 500:
            return False
        try:
            body = response.json()
        except (TypeError, ValueError):
            return False
        return bool(
            isinstance(body, dict)
            and body.get("ok") is False
            and isinstance(body.get("error_code"), int)
            and not isinstance(body.get("error_code"), bool)
            and int(body["error_code"]) == int(response.status_code)
            and isinstance(body.get("description"), str)
            and bool(str(body["description"]).strip())
        )

    @classmethod
    def _send_was_proven_rejected(cls, exc: Exception) -> bool:
        if isinstance(exc, httpx.ConnectError):
            return True
        return isinstance(exc, httpx.HTTPStatusError) and cls._is_proven_rejection(exc.response)


__all__ = [
    "TelegramStatusMessageManager",
    "TelegramStatusStage",
    "build_chat_operation_progress",
    "render_chat_status",
    "render_engineer_status",
]
