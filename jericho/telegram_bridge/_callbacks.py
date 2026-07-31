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
        # Deny-by-default: chats outside the allowlist cannot trigger actions,
        # UNLESS this chat was already admitted by open registration — a callback
        # only exists because the bot already sent this chat a message with
        # buttons, so it is never the first contact; no need to re-derive
        # "private" here, `registered_chats` is the record of that admission.
        if chat_id not in self.config.allowed_chat_ids and not self._inbox.is_registered_chat(chat_id):
            await self._answer_callback(telegram, callback_id, "Действие недоступно", alert=True)
            return

        parts = data.split(":", 2)
        clear_markup = False
        pressed_family = ""
        try:
            if len(parts) != 3:
                raise PermanentUpdateError("Unknown callback action")
            family, action, target_id = parts
            pressed_family = family
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
            elif family == "ent" and action == "browse":
                # Выбор из однофамильцев под /browse. Один вызов отдаёт и имя, и
                # связанные записи — второй поход за знаниями не нужен.
                data_entity = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/kg/entities/{target_id}",
                    None,
                    external_user_id,
                    str(chat_id),
                )
                raw_entity = data_entity.get("entity")
                entity: dict[str, Any] = raw_entity if isinstance(raw_entity, dict) else {}
                raw_knowledge = data_entity.get("knowledge")
                knowledge = raw_knowledge if isinstance(raw_knowledge, list) else []
                name = str(entity.get("name") or "сущность")
                await self._answer_callback(telegram, callback_id, "Показываю")
                if knowledge:
                    await self._send_message(
                        telegram,
                        chat_id,
                        self._format_browse_results(f"Записи «{name}»", knowledge[:8]),
                    )
                else:
                    await self._send_message(
                        telegram,
                        chat_id,
                        f"«{name}» найдена, но подтверждённых записей у неё пока нет.",
                    )
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
            elif family == "conflict" and action in {"dismiss", "keep_a", "keep_b"}:
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/kg/conflicts/{target_id}/decide",
                    {"decision": action, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                toast = {
                    "dismiss": "Отмечено: не конфликт",
                    "keep_a": "Оставлена первая запись",
                    "keep_b": "Оставлена вторая запись",
                }[action]
                await self._answer_callback(telegram, callback_id, toast)
                clear_markup = True
            elif family == "conv" and action in {"delete", "keep"}:
                # G18b: confirm hard-delete of the chat's current conversation.
                # `current` is the same channel-session sentinel as /archive.
                if action == "keep":
                    await self._answer_callback(telegram, callback_id, "Не удаляю")
                    await self._send_message(telegram, chat_id, "Удаление отменено.")
                    clear_markup = True
                else:
                    await self._backend_json(
                        backend,
                        "DELETE",
                        f"/api/conversations/{target_id}",
                        {"telegram_user": user},
                        external_user_id,
                        str(chat_id),
                    )
                    await self._answer_callback(telegram, callback_id, "Удалено")
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Разговор удалён. Следующее сообщение начнёт новый; база знаний не тронута.",
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
                await self._retire_markup_family(
                    telegram, chat_id, telegram_message_id, message, pressed_family
                )

    async def _retire_markup_family(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        message_id: int,
        message: dict[str, Any],
        family: str,
    ) -> None:
        """Убрать отработавший ряд кнопок, не унося чужие.

        Клавиатура ответа несёт до трёх НЕЗАВИСИМЫХ рядов (оценка, отправка в
        Inbox, подтверждение знания), а очистка стирала их разом: 👍 уносил с
        собой «✓ Подтвердить знание», и заметка зависала в pending, пока владелец
        не вспомнит про /inbox. Ряды с кнопками нажатой семьи снимаются, остальные
        перерисовываются на месте; без разобранной семьи (неизвестное действие)
        поведение прежнее — снять всё.
        """
        remaining: list[Any] = []
        raw_markup = message.get("reply_markup")
        markup: dict[str, Any] = raw_markup if isinstance(raw_markup, dict) else {}
        if family:
            for row in markup.get("inline_keyboard") or []:
                if not isinstance(row, list):
                    continue
                if any(
                    str(button.get("callback_data") or "").startswith(f"{family}:")
                    for button in row
                    if isinstance(button, dict)
                ):
                    continue
                remaining.append(row)
        if not remaining:
            await self._clear_inline_markup(client, chat_id, message_id)
            return
        try:
            response = await client.post(
                f"{self._api_url}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": remaining},
                },
            )
            response.raise_for_status()
        except Exception:
            # Косметика: действие уже выполнено, перерисовка не стоит ретрая.
            LOGGER.debug("Could not edit Telegram inline keyboard", exc_info=True)

    @staticmethod
    def _citation_open_buttons(citations: Any) -> list[dict[str, str]]:
        """Кнопки «открыть источник» из легенды ответа.

        Легенда `📎 Источники` показывала метки и заголовки, но из ответа чата
        до самого документа пути не было — кнопки `doc:show` жили только у
        `/search` и `/browse`. Тот же callback уже умеет открыть запись целиком.
        """
        if not isinstance(citations, list):
            return []
        buttons: list[dict[str, str]] = []
        for item in citations:
            if not isinstance(item, dict):
                continue
            knowledge_id = str(item.get("knowledge_id") or "")
            if not knowledge_id or not CALLBACK_TARGET_RE.fullmatch(knowledge_id):
                continue
            label = str(item.get("label") or "").strip()
            text = label if label else str(len(buttons) + 1)
            # Telegram: 64 байта на callback_data; ko_<hex> + префикс укладываются.
            buttons.append({"text": text, "callback_data": f"doc:show:{knowledge_id}"})
        return buttons

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
        source_buttons = CallbacksMixin._citation_open_buttons(citations)
        # По четыре в ряд — как у поиска и хроники: восемь «K#» в одну строку
        # Telegram сжимает в нечитаемое.
        for index in range(0, len(source_buttons), 4):
            keyboard.append(source_buttons[index : index + 4])
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
        # Предупреждение о безосновательности встаёт ПЕРЕД ответом, а не после него.
        # Замерено на переписке владельца: досье на живого человека в 1645 знаков
        # заканчивалось мягкой строчкой про «нет явных ссылок», и он оценил ответ
        # минусом, а не прочитал оговорку. Всё, что ниже, — легенда и уточнения:
        # их место после текста. Это — условие, на котором текст вообще стоит читать.
        warning = str(response.get("grounding_warning") or "").strip()
        if warning:
            body = f"{warning}\n\n{body}"
        # G17b: /regenerate without the original attachment — same slot as
        # grounding_warning (before the answer). Prefer regenerate_notice when
        # both exist so a model-side grounding line does not hide the file loss.
        regen_notice = str(response.get("regenerate_notice") or "").strip()
        if regen_notice and regen_notice != warning:
            body = f"{regen_notice}\n\n{body}"
        fate = _file_fate_line(response.get("file_ingestion"))
        if fate:
            body = f"{body}\n\n{fate}"
        caution = str(response.get("verification_caution") or "").strip()
        if caution:
            body = f"{body}\n\n{caution}"
        legend = str(response.get("citation_notice") or "").strip()
        if legend:
            body = f"{body}\n\n{legend}"
            # Кнопки doc:show висят под ответом; без этой строки человек не знает,
            # что метки [K#] в легенде — не просто подпись, а то, что можно открыть.
            if CallbacksMixin._citation_open_buttons(response.get("citations")):
                body = f"{body}\nКнопкой ниже — открыть источник целиком."
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
