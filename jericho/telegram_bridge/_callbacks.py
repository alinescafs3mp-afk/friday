"""Telegram bridge: routing an inline-button press back to its action.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    CALLBACK_TARGET_RE,
    LOGGER,
    Any,
    BridgeShared,
    PermanentUpdateError,
    httpx,
)


def _file_fate_line(file_ingestion: Any) -> str:
    """Одна строка о судьбе присланного файла — стал знанием или ждёт разбора.

    Бэкенд честно возвращал `file_ingestion` с каждым файлом, и ни бридж, ни
    модель его не читали: владелец отправлял документ и не знал, попал тот в
    знания или завис в Inbox — а это разные следующие шаги (спрашивать можно
    сразу или сначала подтвердить в /inbox).
    """
    if not isinstance(file_ingestion, dict):
        return ""
    if file_ingestion.get("action") == "transient":
        return "📄 Файл разобран, но по вашей просьбе НЕ сохранён."
    if file_ingestion.get("promoted"):
        return "✅ Файл стал знанием — можно спрашивать."
    if file_ingestion.get("queued_for_review") or file_ingestion.get("inbox_id"):
        line = "📥 Файл ждёт разбора в /inbox — в поиск попадёт после подтверждения."
        if file_ingestion.get("extraction_success") is False:
            line += " Текст извлечь не удалось: я вижу файл, но не его содержимое."
        return line
    return ""


class CallbacksMixin(BridgeShared):
    async def _process_callback_query(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        callback: dict[str, Any],
    ) -> None:
        callback_id = str(callback.get("id") or "")
        raw_user = callback.get("from")
        user: dict[str, Any] = dict(raw_user) if isinstance(raw_user, dict) else {}
        raw_message = callback.get("message")
        message: dict[str, Any] = dict(raw_message) if isinstance(raw_message, dict) else {}
        raw_chat = message.get("chat")
        chat: dict[str, Any] = dict(raw_chat) if isinstance(raw_chat, dict) else {}
        chat_id = int(chat.get("id") or 0)
        telegram_message_id = int(message.get("message_id") or 0)
        external_user_id = str(user.get("id") or "")
        data = str(callback.get("data") or "")
        if not callback_id or not chat_id or not external_user_id.isdigit() or not data:
            raise PermanentUpdateError("Callback query is incomplete")
        # Deny-by-default: chats outside the allowlist cannot trigger actions.
        if chat_id not in self.config.allowed_chat_ids:
            await self._answer_callback(telegram, callback_id, "Действие недоступно", alert=True)
            return

        parts = data.split(":", 2)
        clear_markup = False
        try:
            if len(parts) != 3:
                raise PermanentUpdateError("Unknown callback action")
            family, action, target_id = parts
            if not CALLBACK_TARGET_RE.fullmatch(target_id):
                raise PermanentUpdateError("Invalid callback target")
            if family == "inbox" and action in {"promote", "ignore"}:
                status = "classified" if action == "promote" else "ignored"
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/inbox/{target_id}/classify",
                    {"status": status, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Добавлено в знания" if action == "promote" else "Предложение проигнорировано",
                )
                clear_markup = True
            elif family == "doc" and action == "show":
                # Открыть найденный документ целиком. До этого выдача поиска была
                # ТУПИКОМ: заголовок и 160 знаков, а дальше ни id, ни ссылки, ни
                # номера, на который можно сослаться. Прочитать документ было нельзя
                # ничем, кроме ухода в админку и листания полутора тысяч строк — при
                # том что Telegram основной интерфейс владельца.
                #
                # Читается СВОЙ арендатор тем же маршрутом, что и весь мост, поэтому
                # чужое сюда не попадает: `/api/knowledge/{id}` гейтится правами
                # действующего аккаунта.
                document = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/knowledge/{target_id}",
                    None,
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Открываю")
                await self._send_message(telegram, chat_id, self._format_full_document(document))
            elif family == "feedback" and action in {"up", "down", "search_off"}:
                if action == "search_off":
                    feedback_type, score = "search_quality", -1.0
                    ack = "Учту: поиск нашёл не то"
                else:
                    feedback_type = "answer_usefulness"
                    score = 1.0 if action == "up" else -1.0
                    ack = "Спасибо, учту оценку"
                await self._backend_json(
                    backend,
                    "POST",
                    "/api/feedback",
                    {
                        "target_type": "answer",
                        "target_id": target_id,
                        "feedback_type": feedback_type,
                        "score": score,
                        "context": {"channel": "telegram"},
                        "telegram_user": user,
                    },
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, ack)
                # Search-quality feedback leaves the answer's rating buttons in
                # place; only usefulness votes retire the whole keyboard.
                clear_markup = action != "search_off"
            elif family == "research" and action == "save":
                result = await self._backend_json(
                    backend,
                    "POST",
                    "/api/research/candidates",
                    {"message_id": target_id, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Уже в Inbox" if result.get("idempotent_replay") else "Отправлено в Inbox",
                )
                clear_markup = True
            elif family == "work" and action == "save":
                result = await self._backend_json(
                    backend,
                    "POST",
                    "/api/assistant/candidates",
                    {"message_id": target_id, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Уже в Inbox" if result.get("idempotent_replay") else "Результат отправлен в Inbox",
                )
                clear_markup = True
            elif family == "mission" and action in {"start", "stop"}:
                endpoint = "start" if action == "start" else "stop"
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/missions/{target_id}/{endpoint}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Миссия запущена" if action == "start" else "Миссия остановлена",
                )
                clear_markup = True
            elif family == "merge" and action in {"accept", "reject"}:
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/kg/resolutions/{target_id}/{action}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Сущности объединены" if action == "accept" else "Отмечено: не дубликат",
                )
                clear_markup = True
            else:
                raise PermanentUpdateError("Unknown callback action")
        except PermanentUpdateError as exc:
            await self._answer_callback(telegram, callback_id, "Действие уже недоступно", alert=True)
            clear_markup = True
            LOGGER.info("Telegram callback rejected: %s", exc)
        finally:
            # A transport/backend outage is retryable. Keep the buttons visible
            # until the action succeeds or is known to be permanently invalid.
            if clear_markup:
                await self._clear_inline_markup(telegram, chat_id, telegram_message_id)

    @staticmethod
    def _response_reply_markup(response: dict[str, Any]) -> dict[str, Any] | None:
        message_id = str(response.get("message_id") or "")
        if not message_id:
            return None
        row = [
            {"text": "👍", "callback_data": f"feedback:up:{message_id}"},
            {"text": "👎", "callback_data": f"feedback:down:{message_id}"},
        ]
        # When the answer cited stored records, the user can judge retrieval
        # quality separately from answer usefulness — the only user-facing
        # source of search_quality feedback for the ranking loop.
        citations = response.get("citations")
        if isinstance(citations, list) and citations:
            row.append({"text": "🔎 Поиск мимо", "callback_data": f"feedback:search_off:{message_id}"})
        keyboard: list[list[dict[str, str]]] = [row]
        raw_context = response.get("context")
        context: dict[str, Any] = dict(raw_context) if isinstance(raw_context, dict) else {}
        interaction_mode = str(context.get("interaction_mode") or "")
        if interaction_mode == "research":
            keyboard.append([{"text": "В Inbox на review", "callback_data": f"research:save:{message_id}"}])
        elif interaction_mode == "knowledge_work":
            keyboard.append(
                [{"text": "Сохранить результат в Inbox", "callback_data": f"work:save:{message_id}"}]
            )
        raw_ingestion = response.get("ingestion")
        ingestion: dict[str, Any] = dict(raw_ingestion) if isinstance(raw_ingestion, dict) else {}
        inbox_id = str(ingestion.get("inbox_id") or "")
        if inbox_id:
            keyboard.append(
                [
                    {"text": "✓ Подтвердить знание", "callback_data": f"inbox:promote:{inbox_id}"},
                    {"text": "✕ Игнорировать", "callback_data": f"inbox:ignore:{inbox_id}"},
                ]
            )
        return {"inline_keyboard": keyboard}

    @staticmethod
    def _format_response_message(response: dict[str, Any]) -> str:
        message = str(response.get("message") or "Готово.").strip() or "Готово."
        raw_context = response.get("context")
        context: dict[str, Any] = dict(raw_context) if isinstance(raw_context, dict) else {}
        mode = str(context.get("interaction_mode") or "dialogue")
        prefix = {
            "knowledge_work": "🧭 Работа со знаниями",
            "research": "🔎 Исследование",
        }.get(mode)
        body = f"{prefix}\n\n{message}" if prefix else message
        fate = _file_fate_line(response.get("file_ingestion"))
        if fate:
            body = f"{body}\n\n{fate}"
        caution = str(response.get("verification_caution") or "").strip()
        if caution:
            body = f"{body}\n\n{caution}"
        legend = str(response.get("citation_notice") or "").strip()
        if legend:
            body = f"{body}\n\n{legend}"
        return body

    async def _answer_callback(
        self,
        client: httpx.AsyncClient,
        callback_id: str,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        """Acknowledge the button press. Never raises — the action already ran.

        This is a UI acknowledgement, not a state change, and it happens AFTER
        the backend call it acknowledges. Letting it raise made the whole update
        retryable, so the side effect was replayed on every retry: a callback id
        expires (Telegram answers 400 «query is too old»), and one press of 👍
        could re-POST `/api/feedback` up to 288 times over ~24 hours. The audit
        trail filled with duplicates; the answer's rating did not change, because
        `feedback_state` is upserted per (user, target, type) — but "the visible
        damage is small" is not a reason to retry an action that succeeded.

        Same shape as `_clear_inline_markup` two methods down, and for the same
        reason: cosmetic follow-ups must not undo settled work.
        """
        try:
            response = await client.post(
                f"{self._api_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
            )
            response.raise_for_status()
        except Exception:
            LOGGER.info("Could not answer Telegram callback query %s", callback_id, exc_info=True)

    async def _clear_inline_markup(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        message_id: int,
    ) -> None:
        if not chat_id or not message_id:
            return
        try:
            response = await client.post(
                f"{self._api_url}/editMessageReplyMarkup",
                json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            )
            response.raise_for_status()
        except Exception:
            LOGGER.debug("Could not clear Telegram inline keyboard", exc_info=True)
