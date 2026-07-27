"""Telegram bridge: routing an incoming message to the command it names.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    Any,
    BridgeShared,
    MediaTooLargeError,
    PermanentUpdateError,
    asyncio,
    httpx,
)


class CommandsMixin(BridgeShared):
    async def _process_update(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        update: dict[str, Any],
        *,
        cached_response: dict[str, Any] | None,
    ) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._process_callback_query(telegram, backend, callback)
            return

        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_value = message.get("chat")
        user_value = message.get("from")
        chat: dict[str, Any] = chat_value if isinstance(chat_value, dict) else {}
        user: dict[str, Any] = user_value if isinstance(user_value, dict) else {}
        chat_id = int(chat.get("id", 0))
        external_user_id = str(user.get("id") or "")
        if not chat_id or not external_user_id.isdigit():
            raise PermanentUpdateError("Message has no valid chat or user id")
        # Deny-by-default: silently drop chats outside the allowlist so the bot
        # does not act as an open reflector for unknown senders.
        if chat_id not in self.config.allowed_chat_ids:
            return

        text = str(message.get("text") or message.get("caption") or "").strip()
        # Command and argument come from the SAME split. They used to disagree: the
        # command was found with `split(maxsplit=1)` (any whitespace, so a newline
        # counted) while the argument was taken with `partition(" ")` (a literal
        # space only). "/note\nПароли\nrouter: 12345" therefore matched /note and
        # saved the argument "12345" — the note itself silently discarded, the
        # fragment canonicalised as knowledge. "/note\nfoo" produced an empty
        # argument and the usage message. A multi-line note is the normal way to
        # send one from a phone.
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].casefold() if text.startswith("/") else ""
        argument = parts[1].strip() if command and len(parts) > 1 else ""

        async def register_backend_user() -> None:
            # Registration is an authentication side effect on the backend.
            # Normal chat/status/reset requests already perform it, so probe
            # only for bridge-local or unsupported messages.
            await self._backend_json(
                backend,
                "GET",
                "/api/me",
                {"telegram_user": user},
                external_user_id,
                str(chat_id),
            )

        if command == "/start":
            await register_backend_user()
            await self._send_message(
                telegram,
                chat_id,
                "Привет! Я Jericho — локальная система личных знаний. Отправьте заметку, "
                "вопрос, изображение, документ, голосовое, аудио или видео, геолокацию или "
                "контакт. Аудио и видео сохраняются как есть (без расшифровки) и ждут вашего "
                "решения в Inbox. Спорные знания и связи останутся на ваше подтверждение.\n\n"
                "/help — команды",
            )
            return
        if command == "/help":
            await register_backend_user()
            await self._send_message(
                telegram,
                chat_id,
                "Команды:\n"
                "/chat — обычный разговор\n"
                "/work — работа с личными знаниями\n"
                "/research — многошаговое исследование\n"
                "/mission цель — многошаговая миссия в фоне\n"
                "/missions — список миссий и управление\n"
                "/inbox — разобрать ближайшие предложения\n"
                "/merges — подтвердить или отклонить объединение дубликатов\n"
                "/tags — теги базы знаний с количеством записей\n"
                "/browse тег или название — записи по тегу, проекту или сущности\n"
                "/search запрос — найти записи по смыслу, без ответа модели\n"
                "/status — состояние базы\n"
                "/new — начать новый диалог\n"
                "/note текст — явно сохранить заметку\n\n"
                "Ответы можно оценивать кнопками, а результаты /work, /research и миссий — "
                "отправлять в Inbox на review.",
            )
            return
        if command in {"/chat", "/work", "/research"}:
            mode = {
                "/chat": "dialogue",
                "/work": "knowledge_work",
                "/research": "research",
            }[command]
            data = await self._backend_json(
                backend,
                "POST",
                "/api/conversations/channel/mode",
                {
                    "channel": "telegram",
                    "channel_id": str(chat_id),
                    "mode": mode,
                    "telegram_user": user,
                },
                external_user_id,
                str(chat_id),
            )
            labels = {
                "dialogue": "Обычный диалог",
                "knowledge_work": "Работа со знаниями",
                "research": "Исследование",
            }
            await self._send_message(
                telegram,
                chat_id,
                f"Режим: {labels.get(str(data.get('mode')), mode)}.",
            )
            return
        if command == "/inbox":
            await self._send_inbox(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/merges":
            await self._send_merges(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/tags":
            await self._send_tags(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/browse":
            query = argument
            await self._send_browse(telegram, backend, chat_id, external_user_id, user, query)
            return
        if command == "/search":
            query = argument
            await self._send_search(telegram, backend, chat_id, external_user_id, user, query)
            return
        if command == "/new":
            reset_payload = {
                "channel": "telegram",
                "channel_id": str(chat_id),
                "telegram_user": user,
            }
            await self._backend_json(
                backend,
                "POST",
                "/api/conversations/channel/reset",
                reset_payload,
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                "Новый диалог начат в обычном режиме. Сама база знаний не очищена.",
            )
            return
        if command == "/mission":
            goal = argument
            if not goal:
                await register_backend_user()
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /mission цель миссии\n\nЯ разобью её на шаги и, при "
                    "включённой автономии, начну выполнять в фоне. Итоги придут в Inbox на review.",
                )
                return
            created = await self._backend_json(
                backend,
                "POST",
                "/api/missions",
                {"goal": goal, "telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            raw_mission = created.get("mission")
            mission: dict[str, Any] = raw_mission if isinstance(raw_mission, dict) else {}
            await self._send_message(
                telegram,
                chat_id,
                self._format_mission_created(mission),
            )
            return
        if command == "/missions":
            await self._send_missions(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/status":
            data = await self._backend_json(
                backend,
                "GET",
                "/api/kg/stats",
                None,
                external_user_id,
                str(chat_id),
            )
            mode_label = {
                "dialogue": "обычный диалог",
                "knowledge_work": "работа со знаниями",
                "research": "исследование",
            }.get(str(data.get("interaction_mode") or "dialogue"), "обычный диалог")
            await self._send_message(
                telegram,
                chat_id,
                f"Текущий режим: {mode_label}.\n\n"
                "В вашей базе:\n"
                f"• объектов знаний: {data.get('knowledge_object_count', 0)}\n"
                f"• сущностей: {data.get('entity_count', 0)}\n"
                f"• подтверждённых связей: {data.get('relation_count', 0)}\n"
                f"• во входящих: {data.get('pending_inbox', 0)}\n"
                f"• связей на review: {data.get('pending_relation_candidates', 0)}\n"
                f"• конфликтов на review: {data.get('pending_conflicts', 0)}\n"
                f"• предложений объединить сущности: {data.get('pending_resolutions', 0)}",
            )
            return

        force_knowledge = False
        if command == "/note":
            text = argument
            if not text:
                await register_backend_user()
                await self._send_message(telegram, chat_id, "Использование: /note текст заметки")
                return
            force_knowledge = True

        if cached_response is not None:
            await self._send_message(
                telegram,
                chat_id,
                self._format_response_message(cached_response),
                reply_markup=self._response_reply_markup(cached_response),
            )
            return

        # Forwarded-message provenance travels with the ingested content.
        forward = self._extract_forward(message)
        # Location/venue/contact carry no file and no text; turn them into a note
        # so they are captured instead of silently dropped.
        if not text:
            structured = self._structured_text(message)
            if structured:
                text = structured
                force_knowledge = True

        try:
            document = await self._prepare_document(telegram, message, update)
        except MediaTooLargeError:
            await register_backend_user()
            await self._send_message(
                telegram,
                chat_id,
                "Файл слишком большой — Telegram-медиа превышает допустимый размер и не сохранено.",
            )
            return

        if not text and not document:
            await register_backend_user()
            label = self._unsupported_label(message)
            if label:
                await self._send_message(
                    telegram,
                    chat_id,
                    f"Пока не умею обрабатывать {label} — сообщение не сохранено.",
                )
            # Service/empty messages are acknowledged silently (no dead-letter).
            return

        payload: dict[str, Any] = {
            "message": text,
            "force_knowledge": force_knowledge,
            "source_ref": f"telegram-update:{update['update_id']}",
            "telegram_message_id": message.get("message_id"),
            "telegram_user": user,
        }
        if forward:
            payload["forward"] = forward
        if document:
            payload["document"] = document
        typing_task = asyncio.create_task(self._typing_loop(telegram, chat_id))
        try:
            response = await self._backend_json(
                backend,
                "POST",
                "/api/chat",
                payload,
                external_user_id,
                str(chat_id),
            )
            self._inbox.cache_backend_response(int(update["update_id"]), response)
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
        await self._send_message(
            telegram,
            chat_id,
            self._format_response_message(response),
            reply_markup=self._response_reply_markup(response),
        )
