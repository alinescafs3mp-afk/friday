"""Telegram bridge: routing an inline-button press back to its action.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from contextlib import suppress

from friday.telegram_bridge._base import (
    CALLBACK_TARGET_RE,
    LOGGER,
    Any,
    BridgeShared,
    PermanentUpdateError,
    base64,
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
    # Приёмный путь кладёт исход разбора ВЛОЖЕННЫМ словарём `extraction`, а
    # верхнеуровневый `extraction_success` производит только осмотр без
    # сохранения (`inspect_file_transient`) — и тот уходит веткой выше. То есть
    # предупреждение «текст извлечь не удалось» было физически недостижимо:
    # проверено прогоном настоящего `ingest_file` на .png и на .ogg. Человек
    # присылал картинку или наговаривал вопрос, который не расслышали, и получал
    # «📥 Файл ждёт разбора в /inbox» — ни слова о том, что содержимого не видно.
    if file_ingestion.get("voice_unrecognised"):
        return (
            "🎤 Голос не распознался — я сохранила запись, но слов в ней не разобрала. "
            "Повторите текстом или наговорите ещё раз поближе к микрофону."
        )
    extraction = file_ingestion.get("extraction")
    extraction = extraction if isinstance(extraction, dict) else {}
    text_missing = (
        file_ingestion.get("extraction_success") is False
        or extraction.get("success") is False
        or extraction.get("text_success") is False
    )
    # Разбор без ошибки — ещё не текст. Пустой .txt и .docx, где всё написанное
    # лежит в колонтитуле, приходят с `success=True` и нулём знаков: человеку
    # говорили просто «ждёт разбора», и он не знал, что содержимого не видно.
    nothing_came_out = not text_missing and extraction.get("chars") == 0
    partial = bool(extraction.get("parse_deadline_reached"))
    # Текст не поместился в потолок: принято начало, остальное отброшено.
    over_the_cap = bool(extraction.get("text_truncated"))
    if file_ingestion.get("promoted"):
        line = "✅ Файл стал знанием — можно спрашивать."
        if over_the_cap:
            line += (
                " Документ длиннее, чем помещается целиком, — принято начало;"
                " по концу файла спрашивать бесполезно."
            )
        elif partial:
            pages = int(extraction.get("parse_pages_read") or 0)
            read = f" Прочитано страниц: {pages}." if pages else ""
            line += f" Разбор остановлен по сроку — принято только начало.{read}"
        return line
    if file_ingestion.get("queued_for_review") or file_ingestion.get("inbox_id"):
        line = "📥 Файл ждёт разбора в /inbox — в поиск попадёт после подтверждения."
        if text_missing:
            line += " Текст извлечь не удалось: я вижу файл, но не его содержимое."
        elif over_the_cap:
            line += (
                " Документ длиннее, чем помещается целиком, — принято начало;"
                " по концу файла спрашивать бесполезно."
            )
        elif nothing_came_out:
            line += " Текста в файле не оказалось — разбор прошёл, а содержимого нет."
        elif partial:
            # Успех и полнота — разные вещи: разбор, оборванный по сроку, приходит
            # с `success=True` и частичным текстом. Флаг для этого случая писался
            # в ответ, но не читался ни одним потребителем.
            pages = int(extraction.get("parse_pages_read") or 0)
            read = f" Прочитано страниц: {pages}." if pages else ""
            line += f" Разбор остановлен по сроку — принято только начало.{read}"
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
        if not self._may_message_chat(chat_id):
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
            elif family == "ent" and action == "undo":
                # `target_id` — «{id сущности}.{версия}». Версия едет в кнопке, а не
                # вычисляется здесь заново: между показом карточки и нажатием могла
                # вклиниться другая правка, и «отменить последнюю» отменило бы уже
                # не то, что человек видел на экране. Если версии больше нет —
                # backend ответит 404, и это правильный отказ, а не тихий успех.
                entity_id, _, raw_version = target_id.partition(".")
                if not entity_id or not raw_version.isdigit():
                    raise PermanentUpdateError("Invalid entity undo target")
                restored = await self._backend_json(
                    backend,
                    "POST",
                    f"/api/kg/entities/{entity_id}/restore",
                    {"version": int(raw_version), "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                raw_restored = restored.get("entity") if isinstance(restored, dict) else None
                restored_entity: dict[str, Any] = raw_restored if isinstance(raw_restored, dict) else {}
                name = str(restored_entity.get("name") or "").strip()
                await self._answer_callback(telegram, callback_id, "Правка отменена")
                await self._send_message(
                    telegram,
                    chat_id,
                    (f"Объект возвращён к прежнему состоянию: «{name}»." if name else "Правка отменена.")
                    + " Сам откат — тоже правка, его можно отменить так же.",
                )
                clear_markup = True
            elif family == "ent" and action == "types":
                await self._answer_callback(telegram, callback_id, "Выберите тип")
                await self._send_message(
                    telegram,
                    chat_id,
                    "Каким объектом это является?",
                    reply_markup=self._entity_type_markup(target_id),
                )
            elif family == "ent" and action == "type":
                entity_id, _, new_type = target_id.partition(".")
                allowed = {value for value, _ in self._ENTITY_TYPE_CHOICES}
                if not entity_id or new_type not in allowed:
                    raise PermanentUpdateError("Unknown entity type")
                updated = await self._backend_json(
                    backend,
                    "PATCH",
                    f"/api/kg/entities/{entity_id}",
                    {"entity_type": new_type, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                raw_updated = updated.get("entity") if isinstance(updated, dict) else None
                updated_entity: dict[str, Any] = raw_updated if isinstance(raw_updated, dict) else {}
                if not updated_entity:
                    # Сервер ничего не изменил (объект удалён или это надгробие
                    # слияния). Печатать «Тип изменён» на такой ответ значит врать
                    # человеку о состоянии его же графа.
                    raise PermanentUpdateError("Entity update returned no entity")
                name = str(updated_entity.get("name") or "").strip()
                await self._answer_callback(telegram, callback_id, "Тип изменён")
                await self._send_message(
                    telegram,
                    chat_id,
                    (f"«{name}»: тип теперь «{new_type}». " if name else f"Тип теперь «{new_type}». ")
                    + "Это правка объекта — её можно отменить в его карточке.",
                )
                clear_markup = True
            elif family == "ent" and action == "del":
                # Разрушительное действие подтверждается, и приглашение несёт id
                # ВЫЗВАВШЕГО: в чате с несколькими способными аккаунтами кнопка,
                # показанная одному, не должна срабатывать у другого. Этот же
                # дефект уже ловили состязательным ревью на удалении разговора.
                await self._answer_callback(telegram, callback_id, "Подтвердите")
                await self._send_message(
                    telegram,
                    chat_id,
                    "Удалить объект из графа? Документы и их текст останутся на месте — "
                    "исчезнет только сам узел и его связи. Удаление мягкое.",
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Да, убрать",
                                    "callback_data": f"ent:delyes:{target_id}.{external_user_id}",
                                },
                                {"text": "Нет", "callback_data": f"ent:delno:{target_id}"},
                            ]
                        ]
                    },
                )
            elif family == "ent" and action == "delno":
                await self._answer_callback(telegram, callback_id, "Отменено")
                clear_markup = True
            elif family == "ent" and action == "delyes":
                entity_id, _, invoker = target_id.partition(".")
                if not entity_id or not invoker:
                    raise PermanentUpdateError("Invalid entity delete target")
                if invoker != external_user_id:
                    await self._answer_callback(
                        telegram,
                        callback_id,
                        "Эта кнопка не ваша: удалить может только тот, кто открыл карточку",
                        alert=True,
                    )
                    return
                await self._backend_json(
                    backend,
                    "DELETE",
                    f"/api/kg/entities/{entity_id}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Объект удалён")
                await self._send_message(
                    telegram,
                    chat_id,
                    "Объект удалён из графа. Документы целы: удалён узел, а не то, из чего "
                    "он был извлечён. Вернуть — кнопкой ниже.",
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {
                                    "text": "↩︎ Вернуть объект",
                                    "callback_data": f"ent:undel:{entity_id}.{external_user_id}",
                                }
                            ]
                        ]
                    },
                )
                clear_markup = True
            elif family == "ent" and action == "undel":
                # Обратный ход к удалению — там же, где само удаление, и по тому же
                # правилу «нажимает тот, кто вызвал»: кнопка видна всему чату.
                # Без этой ветки «мягкое удаление» было мягким только на словах:
                # вернуть узел не мог ни один маршрут, а карточка по имени больше
                # не открывалась — то есть кнопки отката было негде нажать.
                entity_id, _, invoker = target_id.partition(".")
                if not entity_id or not invoker:
                    raise PermanentUpdateError("Invalid entity undelete target")
                if invoker != external_user_id:
                    await self._answer_callback(
                        telegram,
                        callback_id,
                        "Эта кнопка не ваша: вернуть может тот, кто удалял",
                        alert=True,
                    )
                    return
                returned = await self._backend_json(
                    backend,
                    "POST",
                    f"/api/kg/entities/{entity_id}/undelete",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                raw_returned = returned.get("entity") if isinstance(returned, dict) else None
                returned_entity: dict[str, Any] = raw_returned if isinstance(raw_returned, dict) else {}
                returned_name = str(returned_entity.get("name") or "").strip()
                await self._answer_callback(telegram, callback_id, "Объект возвращён")
                await self._send_message(
                    telegram,
                    chat_id,
                    f"Объект «{returned_name}» снова в графе." if returned_name else "Объект снова в графе.",
                )
                clear_markup = True
            elif family == "apr" and action in {"yes", "no"}:
                # Решение и исполнение — один запрос: между «человек согласился» и
                # «действие случилось» не должно быть места, где всё замирает.
                decided = await self._backend_json(
                    backend,
                    "POST",
                    f"/api/approvals/{target_id}/decide",
                    {
                        "telegram_user": user,
                        "decision": "approve" if action == "yes" else "reject",
                    },
                    external_user_id,
                    str(chat_id),
                )
                if action == "no":
                    await self._answer_callback(telegram, callback_id, "Отклонено")
                    await self._send_message(telegram, chat_id, "Действие отклонено — оно не выполнено.")
                else:
                    executed = bool(decided.get("executed"))
                    await self._answer_callback(
                        telegram, callback_id, "Выполнено" if executed else "Не выполнено"
                    )
                    # Согласие человека и успех исполнения — разные вещи, и вторую
                    # нельзя выдавать за первую: подтверждённое действие могло не
                    # состояться (право отобрали, аргументы изменились, сбой).
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Готово: действие выполнено."
                        if executed
                        else (
                            "Решение записано, но действие НЕ выполнено: "
                            + (str(decided.get("error") or "").strip() or "причина неизвестна")
                        ),
                    )
                clear_markup = True
            elif family == "mon" and action == "stop":
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/me/monitors/{target_id}/stop",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Слежение снято")
                await self._send_message(
                    telegram, chat_id, "Больше не слежу за этой темой. Список: /watching"
                )
                clear_markup = True
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
                # `current` is the same channel-session sentinel as /archive,
                # but the button also carries the id of whoever /delete was
                # shown to ("current.{invoker_id}") — the prompt is visible to
                # the whole chat, and without this check whichever OTHER
                # capable account tapped it first would delete their own
                # current conversation, not the invoker's (found by
                # adversarial review; live-reachable in any allowlisted group
                # or open-registration chat with more than one account).
                conv_ref, _, invoker_id = target_id.partition(".")
                if not invoker_id or invoker_id != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
                if action == "keep":
                    await self._answer_callback(telegram, callback_id, "Не удаляю")
                    await self._send_message(telegram, chat_id, "Удаление отменено.")
                    clear_markup = True
                else:
                    await self._backend_json(
                        backend,
                        "DELETE",
                        f"/api/conversations/{conv_ref}",
                        {"telegram_user": user},
                        external_user_id,
                        str(chat_id),
                    )
                    await self._answer_callback(telegram, callback_id, "Удалено")
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Разговор убран из списка. Переписка сохранена — сказанное в чате не удаляется. "
                        "Следующее сообщение начнёт новый разговор; база знаний не тронута.",
                    )
                    clear_markup = True
            elif family == "remind" and action == "dismiss":
                # G19: снять напоминание, не очищая dedup_key (делает backend).
                # `target_id` — идентификатор СОБЫТИЯ: список строится по событиям,
                # а не по очереди отправки, которую мост же и опустошает.
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/me/reminders/{target_id}/dismiss",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Снято")
                await self._send_message(telegram, chat_id, "Напоминание снято.")
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
        # Что ушло в поисковик — рядом с ответом, а не в журнале.
        web_notice = str(response.get("web_query_notice") or "").strip()
        if web_notice:
            body = f"{body}\n\n{web_notice}"
        legend = str(response.get("citation_notice") or "").strip()
        if legend:
            body = f"{body}\n\n{legend}"
            # Кнопки doc:show висят под ответом; без этой строки человек не знает,
            # что метки [K#] в легенде — не просто подпись, а то, что можно открыть.
            if CallbacksMixin._citation_open_buttons(response.get("citations")):
                body = f"{body}\nКнопкой ниже — открыть источник целиком."
        return body

    async def _deliver_voice_reply(
        self,
        telegram: httpx.AsyncClient,
        chat_id: int,
        response: dict[str, Any],
    ) -> None:
        """Send the `speak` tool's clip (if this turn produced one) as a native
        Telegram voice bubble, after the text reply. Best-effort: a delivery
        failure here must not turn an otherwise-successful text answer into an
        error for the user, so it is logged and swallowed, not raised.
        """
        voice = response.get("voice")
        if not isinstance(voice, dict):
            return
        encoded = str(voice.get("audio_base64") or "")
        if not encoded:
            return
        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            LOGGER.warning("tts: response carried an unparsable voice attachment")
            return
        try:
            await self._send_voice(telegram, chat_id, audio_bytes)
        except Exception:
            LOGGER.warning("tts: sendVoice failed", exc_info=True)
            return
        if voice.get("truncated"):
            # Ответ не поместился в клип целиком. Молчать об этом нельзя: рядом
            # лежит полный текст, и человек должен знать, что услышал не всё.
            with suppress(Exception):
                await self._send_message(
                    telegram,
                    chat_id,
                    "Ответ длиннее, чем помещается в голосовое, — озвучено начало. "
                    "Полный текст выше.",
                )

    async def _deliver_generated_files(
        self,
        telegram: httpx.AsyncClient,
        chat_id: int,
        response: dict[str, Any],
    ) -> None:
        """Отправить файлы, собранные инструментом `make_file`, после текста.

        Требование владельца: «сделай отчёт» должно заканчиваться файлом. Файл
        уходит документом, а не голосом и не текстом: у `sendDocument` есть имя и
        расширение, по которым человек его откроет.

        Как и голос — по возможности: сбой доставки не должен превращать удачный
        текстовый ответ в ошибку. Но, в отличие от голоса, о неудаче говорится
        человеку: он ПРОСИЛ файл и молчания не поймёт.
        """
        files = response.get("files")
        if not isinstance(files, list):
            return
        for item in files:
            if not isinstance(item, dict):
                continue
            encoded = str(item.get("content_base64") or "")
            if not encoded:
                continue
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                LOGGER.warning("make_file: вложение не разобралось")
                continue
            filename = str(item.get("filename") or "report.bin")
            try:
                await self._send_document(
                    telegram,
                    chat_id,
                    filename,
                    payload,
                    mime_type=str(item.get("mime_type") or "application/octet-stream"),
                )
            except Exception:
                LOGGER.warning("make_file: sendDocument не удался", exc_info=True)
                with suppress(Exception):
                    await self._send_message(
                        telegram,
                        chat_id,
                        f"Файл «{filename}» собран, но отправить его не удалось. Попробуйте попросить ещё раз.",
                    )

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
