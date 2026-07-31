"""Telegram bridge: routing an incoming message to the command it names.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from jericho.retrieval._keyboard import switched
from jericho.telegram_bridge._base import (
    BOT_COMMANDS,
    Any,
    BridgeShared,
    MediaTooLargeError,
    PermanentUpdateError,
    asyncio,
    httpx,
)
from jericho.telegram_bridge._views import _TIMELINE_SHOWN

_MONTHS_RU = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}


def parse_period(argument: str, *, today: date) -> tuple[str, str, str] | None:
    """«март 2023», «2023», «2023-03», «неделя» → (с, по, как назвать). None — не разобрано.

    Разбор намеренно узкий и предсказуемый: человек, чей запрос не поняли, должен
    получить подсказку с примерами, а не молча чужой период. Угадывать «весной» или
    «прошлым летом» — тот же класс ошибки, что придумывать дату документа.
    """
    text = " ".join((argument or "").split()).casefold().strip()
    if not text or text in {"месяц", "за месяц"}:
        start = today - timedelta(days=30)
        return start.isoformat(), today.isoformat(), "за 30 дней"
    if text in {"неделя", "за неделю"}:
        start = today - timedelta(days=7)
        return start.isoformat(), today.isoformat(), "за неделю"
    if text in {"год", "за год"}:
        start = today - timedelta(days=365)
        return start.isoformat(), today.isoformat(), "за год"
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01", f"{text}-12-31", text
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _month_bounds(year, month)
    match = re.fullmatch(r"([а-я]+)\s+(\d{4})", text)
    if match:
        month = next(
            (number for prefix, number in _MONTHS_RU.items() if match.group(1).startswith(prefix)), 0
        )
        if month:
            return _month_bounds(int(match.group(2)), month)
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|—|-)\s*(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1), match.group(2), f"{match.group(1)} — {match.group(2)}"
    return None


def _month_bounds(year: int, month: int) -> tuple[str, str, str]:
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat(), f"{first:%m}.{year}"


class CommandsMixin(BridgeShared):
    @staticmethod
    def _read_command_layout(text: str) -> str:
        """Recognise a command typed without switching the keyboard layout.

        `/inbox` on a Russian layout comes out as «.штищч» — even the slash
        changes, because that key writes a full stop there. So it does not look
        like a command at all and is answered as a chat message, which for
        `/inbox` or `/new` is a confusing non-answer rather than a small mistake.

        Accepted only when the re-reading names a command that actually exists.
        A message legitimately starting with a full stop flips to something
        starting with a slash too, and «...ну ладно» must stay a message.

        ONLY the command word is re-read; the argument is left exactly as typed.
        Routing a message is one thing, rewriting its content is another — and
        `/note` writes that content into the knowledge base, where a wrong guess
        would be stored as the user's own words. If the argument was mistyped
        too, the search path repairs it at query time and a note shows the user
        their own text to resend.
        """
        if text.startswith("/") or not text:
            return text
        head = text.split(maxsplit=1)[0] if text.split() else ""
        flipped = switched(head)
        if not head or not flipped.startswith("/"):
            return text
        name = flipped.split("@", 1)[0].casefold().lstrip("/")
        if name not in {command for command, _ in BOT_COMMANDS}:
            return text
        # The remainder is sliced, not re-joined: whitespace is part of the
        # argument. A note sent from a phone is multi-line, and rebuilding it
        # around a single space is the exact defect `_process_update` documents.
        return flipped + text[len(head) :]

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

        edited = update.get("edited_message")
        if isinstance(edited, dict):
            # Правка сообщения НЕ подхватывается: заметка уже ушла в базу со старым
            # текстом (идемпотентность держится за source_ref обновления), и молчание
            # означало бы, что в чате один текст, а в архиве навсегда другой — и
            # владелец об этом не знает.
            raw_chat = edited.get("chat")
            edited_chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
            edited_chat_id = int(edited_chat.get("id") or 0)
            if edited_chat_id in self.config.allowed_chat_ids:
                await self._send_message(
                    telegram,
                    edited_chat_id,
                    "Правку сообщения я не подхватываю: сохранён исходный текст. "
                    "Пришлите исправление новым сообщением (для заметок — /note).",
                )
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
        # does not act as an open reflector for unknown senders. The ONE
        # exception is a private chat when open registration is on: this is the
        # single admission point — every later check (callbacks, outbound push)
        # trusts `registered_chats`, populated only here, only for a chat
        # Telegram itself labels "private" (never a group/supergroup/channel).
        if chat_id not in self.config.allowed_chat_ids:
            is_private = str(chat.get("type") or "") == "private"
            if not (self.config.open_registration and is_private):
                return
            self._inbox.remember_registered_chat(chat_id)

        text = str(message.get("text") or message.get("caption") or "").strip()
        # Command and argument come from the SAME split. They used to disagree: the
        # command was found with `split(maxsplit=1)` (any whitespace, so a newline
        # counted) while the argument was taken with `partition(" ")` (a literal
        # space only). "/note\nПароли\nrouter: 12345" therefore matched /note and
        # saved the argument "12345" — the note itself silently discarded, the
        # fragment canonicalised as knowledge. "/note\nfoo" produced an empty
        # argument and the usage message. A multi-line note is the normal way to
        # send one from a phone.
        text = self._read_command_layout(text)
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].casefold() if text.startswith("/") else ""
        argument = parts[1].strip() if command and len(parts) > 1 else ""

        async def register_backend_user() -> dict[str, Any]:
            # Registration is an authentication side effect on the backend.
            # Normal chat/status/reset requests already perform it, so probe
            # only for bridge-local or unsupported messages. The /api/me payload
            # is also the only place the bridge learns the account's preset, so
            # /start can tell a newcomer what they can (and cannot) do.
            data = await self._backend_json(
                backend,
                "GET",
                "/api/me",
                {"telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            return data if isinstance(data, dict) else {}

        if command == "/start":
            me = await register_backend_user()
            # Byte-stable welcome for every non-newcomer account: owner, guest,
            # user, linked identity. Only the open-registration newcomer path
            # appends the limitation note — so existing /start tests keep their
            # exact text and a mutation that drops the branch turns red on the
            # newcomer-specific assertion alone.
            start_text = (
                "Привет! Я Jericho — локальная система личных знаний. Отправьте заметку, "
                "вопрос, изображение, документ, голосовое, аудио или видео, геолокацию или "
                "контакт. Аудио и видео сохраняются как есть (без расшифровки) и ждут вашего "
                "решения в Inbox. Спорные знания и связи останутся на ваше подтверждение.\n\n"
                "/help — команды"
            )
            actor = me.get("actor") if isinstance(me.get("actor"), dict) else {}
            if str(actor.get("preset_key") or "") == "newcomer":
                start_text = (
                    f"{start_text}\n\n"
                    "Сейчас у вас режим новичка: чат, файлы и веб-поиск доступны, "
                    "миссии и выполнение кода — нет. Владелец может расширить доступ."
                )
            await self._send_message(telegram, chat_id, start_text)
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
                "/conflicts — разобрать конфликты знаний (порциями)\n"
                "/merges — подтвердить или отклонить объединение дубликатов\n"
                "/tags — теги базы знаний с количеством записей\n"
                "/browse тег или название — записи по тегу, проекту или сущности\n"
                "/search запрос — найти записи по смыслу, без ответа модели\n"
                "/status — состояние базы\n"
                "/why — почему был такой ответ\n"
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
        if command == "/conflicts":
            await self._send_conflicts(telegram, backend, chat_id, external_user_id, user)
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
        if command == "/timeline":
            # Хроника архива в чате. Стала возможна только теперь: до появления
            # собственной даты документа у всего корпуса была одна дата — день
            # импорта, и лента показывала бы один день на полторы тысячи записей.
            period = parse_period(argument, today=date.today())
            if period is None:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Не понял период. Примеры: /timeline 2023, /timeline март 2023, "
                    "/timeline 2023-03, /timeline 2020-01-01..2020-03-31, /timeline неделя. "
                    "Без периода — за последние 30 дней.",
                )
                return
            since, until, label = period
            documents = await self._backend_json(
                backend,
                "GET",
                # Просим на один больше, чем покажем: лишний нужен, чтобы честно сказать
                # «показаны первые N из M», а не молча обрезать. Больше просить незачем —
                # точное число за период человек увидит на экране «Хроника».
                f"/api/knowledge/by-date?since={since}&until={until}&limit={_TIMELINE_SHOWN + 1}",
                None,
                external_user_id,
                str(chat_id),
            )
            events = await self._backend_json(
                backend,
                "GET",
                f"/api/kg/timeline?start={since}&end={until}&limit=15",
                None,
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                self._format_timeline(label, documents, events),
                reply_markup=self._timeline_reply_markup(documents),
            )
            return
        if command == "/source":
            # Дословный поиск по ИСХОДНЫМ файлам. 93% загруженных знаков живут
            # только в raw_objects, и когда ревью сжало документ до сводки, фраза
            # из PDF была находима лишь с хоста через админку.
            if not argument:
                await self._send_message(telegram, chat_id, "Использование: /source <фраза из документа>")
                return
            from urllib.parse import quote

            found = await self._backend_json(
                backend,
                "GET",
                f"/api/knowledge/sources?q={quote(argument, safe='')}&limit=5",
                None,
                external_user_id,
                str(chat_id),
            )
            raw_items = found.get("items") if isinstance(found.get("items"), list) else []
            if not raw_items:
                await self._send_message(
                    telegram,
                    chat_id,
                    f"Дословно «{argument}» в исходных файлах не нашлось. Это не значит, "
                    "что фразы не было вовсе: отвергнутые в /inbox материалы не ищутся.",
                )
                return
            lines = [f"Исходные файлы с «{argument}»:"]
            buttons: list[dict[str, str]] = []
            for index, item in enumerate(raw_items, start=1):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("source_ref") or item.get("source") or "без имени")[:80]
                excerpt = str(item.get("excerpt") or "").replace("\n", " ").strip()[:160]
                lines.append(f"{index}. {label}")
                if excerpt:
                    lines.append(f"   {excerpt}")
                knowledge_id = str(item.get("knowledge_object_id") or "")
                if knowledge_id:
                    buttons.append({"text": str(index), "callback_data": f"doc:show:{knowledge_id}"})
            markup = {"inline_keyboard": [buttons]} if buttons else None
            if buttons:
                lines.append("")
                lines.append("Кнопкой ниже — открыть запись, выросшую из файла.")
            await self._send_message(telegram, chat_id, "\n".join(lines), reply_markup=markup)
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
        if command == "/why":
            try:
                data = await self._backend_json(
                    backend,
                    "GET",
                    "/api/conversations/channel/why",
                    None,
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError:
                # 404 «в этом канале ещё нет диалога/ответа» — не сбой, а честное
                # «пока нечего объяснять». Раньше это уходило в dead-letter, и
                # человек после /new получал «⚠️ сообщение отклонено» — текст,
                # написанный для настоящих отказов.
                await self._send_message(
                    telegram,
                    chat_id,
                    "Пока нечего объяснять: в этом чате ещё не было ответа. "
                    "Задайте вопрос — и /why покажет, как я его искал.",
                )
                return
            trace = data.get("trace") or []
            dropped = [item for item in trace if item.get("reason")]
            lines = [
                f"Запрос, который реально выполнялся: «{data.get('search_query') or '—'}»",
                f"Режим: {data.get('answer_mode') or '—'}; найдено записей: {data.get('knowledge_hits', 0)}",
            ]
            citations = data.get("citations") or {}
            if citations:
                # Названия записей есть в трейсе; голая метка «K2» бесполезна,
                # когда сообщение с легендой 📎 уже уехало вверх по чату. Сортировка
                # по (длина, метка) даёт числовой порядок: K1, K2, …, K10.
                titles = {
                    str(item.get("id") or ""): str(item.get("title") or "")
                    for item in trace
                    if isinstance(item, dict)
                }
                named = []
                for label in sorted(citations, key=lambda mark: (len(mark), mark)):
                    title = titles.get(str(citations.get(label) or ""), "")[:60]
                    named.append(f"[{label}] {title}" if title else f"[{label}]")
                lines.append("Источники: " + "; ".join(named))
            if dropped:
                lines.append("")
                lines.append("Отброшено при ранжировании:")
                for item in dropped[:8]:
                    title = str(item.get("title") or item.get("id") or "")[:60]
                    lines.append(f"• {title} — {item.get('reason')}")
            elif trace:
                lines.append("")
                lines.append("Ничего не отбрасывалось: всё, что нашлось, попало в ответ.")
            else:
                lines.append("")
                lines.append("Трассировки нет — последний ответ шёл без поиска по базе.")
            await self._send_message(telegram, chat_id, "\n".join(lines))
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
                self._format_status(mode_label, data),
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
        except MediaTooLargeError as exc:
            await register_backend_user()
            # The exception text names the ACTUAL ceiling (Telegram's 20 MB or the
            # configured limit) — the sender's next step depends on which it was.
            await self._send_message(
                telegram,
                chat_id,
                str(exc)
                or "Файл слишком большой — Telegram-медиа превышает допустимый размер и не сохранено.",
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
