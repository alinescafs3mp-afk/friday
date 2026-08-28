"""One durable, monotonically edited Telegram status message per operation."""

from __future__ import annotations

import asyncio
import re
import weakref
from enum import Enum
from typing import Any

import httpx

from friday.telegram_bridge._base import LOGGER, TELEGRAM_TEXT_LIMIT, split_for_telegram

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


_RUNNING_STAGE_LABELS = {
    TelegramStatusStage.RECEIVING_MEDIA: "получаю вложения из Telegram",
    TelegramStatusStage.STAGING_DOCUMENTS: "передаю вложения в ядро",
    TelegramStatusStage.BACKEND_WAIT: "ядро обрабатывает запрос",
    TelegramStatusStage.DELIVERING_RESULT: "отправляю готовый результат",
}


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


def render_chat_status(
    stage: TelegramStatusStage,
    elapsed_sec: float,
    *,
    item_total: int = 0,
    received_items: int = 0,
    received_bytes: int = 0,
    staged_items: int = 0,
    staged_bytes: int = 0,
) -> str:
    """Render only exact elapsed time and a stage the bridge can observe."""

    elapsed = _elapsed_label(elapsed_sec)
    facts: list[str] = []
    if item_total:
        facts.append(
            f"Получено вложений: {max(0, received_items)} из {max(0, item_total)}, "
            f"{_bytes_label(received_bytes)}."
        )
        if staged_items or stage in {
            TelegramStatusStage.STAGING_DOCUMENTS,
            TelegramStatusStage.DELIVERING_RESULT,
            TelegramStatusStage.COMPLETE,
        }:
            facts.append(
                f"Принято ядром: {max(0, staged_items)} из {max(0, item_total)}, "
                f"{_bytes_label(staged_bytes)}."
            )
    suffix = "\n" + " ".join(facts) if facts else ""
    if stage is TelegramStatusStage.COMPLETE:
        return f"✅ Запрос завершён за {elapsed}. Результат отправлен.{suffix}"
    if stage is TelegramStatusStage.STOPPED:
        return f"⏹ Обработка остановлена через {elapsed}.{suffix}"
    return f"⏳ Запрос выполняется {elapsed}.\nЭтап: {_RUNNING_STAGE_LABELS[stage]}.{suffix}"


def render_engineer_status(update: dict[str, Any]) -> str:
    """Render a validated, content-free Engineer status carrier."""

    stage = str(update["stage"])
    job_id = str(update["operation_id"]).removeprefix("engineer:")
    if bool(update["terminal"]):
        terminal_text = {
            "completed": "✅ Engineer-задача завершена. Результат отправлен.",
            "failed": "❌ Engineer-задача завершилась с ошибкой. Итог отправлен.",
            "cancelled": "⏹ Engineer-задача отменена. Итог отправлен.",
            "timeout": "⏱ Engineer-задача остановлена по тайм-ауту. Итог отправлен.",
            "unknown": "⚠️ Состояние Engineer-задачи неизвестно. Ни успех, ни ошибка не подтверждены.",
            "delivery_uncertain": ("⚠️ Engineer-задача завершена; доставка результата не подтверждена."),
        }[stage]
        return f"{terminal_text}\nJob: {job_id}."
    elapsed = _elapsed_label(float(update["elapsed_sec"]))
    stdout_bytes = int(update["stdout_bytes"])
    stderr_bytes = int(update["stderr_bytes"])
    output = (
        f"Вывод на контрольном замере: stdout {_bytes_label(stdout_bytes)}, "
        f"stderr {_bytes_label(stderr_bytes)}."
        if bool(update["output_activity"])
        else "Текстового вывода на контрольном замере ещё не было."
    )
    timeout_sec = int(update["timeout_sec"])
    if timeout_sec:
        deadline = (
            f"Настроенный тайм-аут: {_elapsed_label(timeout_sec)}; "
            f"на момент замера оставалось {_elapsed_label(int(update['remaining_sec']))}."
        )
    else:
        deadline = "Жёсткий тайм-аут не задан."
    return (
        f"⏳ Engineer-задача выполняется. Контрольный замер: {elapsed}.\n"
        f"Job: {job_id}. "
        f"Этап: выполняется команда. {output} {deadline}"
    )


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
    "render_chat_status",
    "render_engineer_status",
]
