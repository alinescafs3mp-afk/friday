"""Telegram bridge: routing an incoming message to the command it names.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

from friday.organs.engineer.targets import requests_artifact_decompile
from friday.retrieval._keyboard import switched
from friday.telegram_bridge._base import (
    BOT_COMMANDS,
    CALLBACK_TARGET_RE,
    LOGGER,
    Any,
    BridgeShared,
    MediaTooLargeError,
    PermanentUpdateError,
    asyncio,
    httpx,
    quote,
    refusal_notice,
)
from friday.telegram_bridge._media import _reply_document_file_unique_id
from friday.telegram_bridge._obsidian import obsidian_panel
from friday.telegram_bridge._views import _TIMELINE_SHOWN


def _split_rename(argument: str) -> tuple[str, str]:
    """«старое имя => новое имя» → две части.

    Отдельной функцией, а не внутри разбора команд: там структурный сторож
    (`test_bridge_surface`) запрещает `partition`, потому что однажды им уже
    разделили команду и аргумент по литеральному пробелу — и многострочная
    команда потеряла всё, кроме первой строки. Здесь стрелка, а не пробел, и это
    другой разбор, но правило пусть остаётся простым: в разборе команд —
    никаких `partition`.

    Стрелка нужна потому, что имена содержат пробелы: ФИО целиком, «в/ч 12345».
    """
    left, separator, right = argument.partition("=>")
    if not separator:
        return "", ""
    return left.strip(), right.strip()


def _response_text_format(response: dict[str, Any]) -> str:
    """Carry only the backend's closed, code-owned plain-text provenance."""

    return "plain" if response.get("message_format") == "plain" else "markdown"


_ALBUM_CACHE_MESSAGE_IDS = "_friday_album_message_ids"

# These are deliberately sparse and finite. Telegram's typing indicator says
# only that the bridge process is alive; after a minute it gives the person no
# clue whether a long backend turn is still owned. The bridge observes elapsed
# request time, not a Ghidra phase event, so it must never invent completion
# percentages or claim that a particular worker stage has been entered.
_DECOMPILE_PROGRESS_SCHEDULE: tuple[tuple[float, str], ...] = (
    (
        12.0,
        "⏳ Запрос на статический разбор ещё выполняется. Длительная фаза ограничена "
        "четырьмя минутами; итог пришлю сразу после завершения.",
    ),
    (
        75.0,
        "⏳ Запрос на статический разбор всё ещё выполняется. Прошло около минуты; "
        "продолжаю ждать ограниченный backend-ход.",
    ),
)
_GENERIC_PROGRESS_SCHEDULE: tuple[tuple[float, str], ...] = (
    (
        30.0,
        "⏳ Запрос ещё выполняется. Продолжаю работу; итог пришлю сразу после завершения.",
    ),
)
# A module seam keeps progress timing deterministic in focused async tests while
# production still uses the normal event-loop clock.
_progress_sleep = asyncio.sleep


class _ChatProgressState:
    """One finite notification budget shared by every final call in a turn."""

    __slots__ = ("next_notice", "schedule")

    def __init__(self, speech: str) -> None:
        self.schedule = (
            _DECOMPILE_PROGRESS_SCHEDULE
            if requests_artifact_decompile(speech)
            else _GENERIC_PROGRESS_SCHEDULE
        )
        self.next_notice = 0


async def _emit_chat_progress(
    bridge: BridgeShared,
    telegram: httpx.AsyncClient,
    chat_id: int,
    reply_to_message_id: int | None,
    state: _ChatProgressState,
) -> None:
    """Emit only the remaining best-effort checkpoints for one final call."""

    previous_delay = 0.0
    for index in range(state.next_notice, len(state.schedule)):
        delay, notice = state.schedule[index]
        await _progress_sleep(max(0.0, delay - previous_delay))
        previous_delay = delay
        # Spend the slot before attempting delivery: a Telegram outage must not
        # turn a bounded status channel into a retry loop or affect the request.
        state.next_notice = index + 1
        try:
            await bridge._send_message(
                telegram,
                chat_id,
                notice,
                text_format="plain",
                reply_to_message_id=reply_to_message_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.debug("Telegram progress notice failed (%s)", type(exc).__name__)


async def _final_chat_request_with_progress(
    bridge: BridgeShared,
    telegram: httpx.AsyncClient,
    backend: httpx.AsyncClient,
    payload: dict[str, Any],
    external_user_id: str,
    chat_id: int,
    reply_to_message_id: int | None,
    state: _ChatProgressState,
) -> dict[str, Any]:
    """Wrap one final /api/chat call without changing its request semantics."""

    progress_task = asyncio.create_task(
        _emit_chat_progress(
            bridge,
            telegram,
            chat_id,
            reply_to_message_id,
            state,
        )
    )
    try:
        return await bridge._backend_json(
            backend,
            "POST",
            "/api/chat",
            payload,
            external_user_id,
            str(chat_id),
        )
    finally:
        # The notifier is completely stopped before the response/error leaves
        # this helper, so a stale status cannot trail the final answer.
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)


def _telegram_item_receipt(document: dict[str, Any]) -> dict[str, Any] | None:
    """Build the exact bounded acknowledgement expected for one album sibling."""

    message_id = document.get("telegram_message_id")
    source_ref = document.get("source_ref")
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or not 0 < message_id <= (2**63 - 1)
        or not isinstance(source_ref, str)
        or not source_ref
    ):
        return None
    return {
        "telegram_message_id": message_id,
        "source_ref_sha256": hashlib.sha256(source_ref.encode("utf-8")).hexdigest(),
    }


def _album_final_source_ref(
    update_id: int,
    album_messages: list[dict[str, Any]],
    prepared_documents: list[dict[str, Any]],
) -> str:
    """Bind a v2 final turn to the exact ordered album, never its old anchor key."""

    prepared_by_message_id: dict[int, str] = {}
    for document in prepared_documents:
        receipt = _telegram_item_receipt(document)
        if receipt is None:
            continue
        message_id = int(receipt["telegram_message_id"])
        if message_id in prepared_by_message_id:
            raise RuntimeError("Telegram album has duplicate prepared message identity")
        prepared_by_message_id[message_id] = str(receipt["source_ref_sha256"])

    ordered_items: list[dict[str, Any]] = []
    for message in album_messages:
        ordered_message_id = message.get("message_id")
        if (
            not isinstance(ordered_message_id, int)
            or isinstance(ordered_message_id, bool)
            or not 0 < ordered_message_id <= (2**63 - 1)
        ):
            raise RuntimeError("Telegram album has invalid ordered message identity")
        ordered_items.append(
            {
                "telegram_message_id": ordered_message_id,
                # An item rejected before preparation has no file authority in
                # the final turn.  The empty digest is an explicit terminal
                # marker and remains bound to its ordered Telegram identity.
                "source_ref_sha256": prepared_by_message_id.get(ordered_message_id, ""),
            }
        )
    canonical = json.dumps(ordered_items, ensure_ascii=True, separators=(",", ":"))
    album_digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return f"telegram-album-v2:{update_id}:{album_digest}"


def _prepared_document_size(document: dict[str, Any]) -> int:
    """Return exact decoded size for bridge-generated canonical base64."""

    encoded = document.get("content_base64")
    if not isinstance(encoded, str) or not encoded or len(encoded) % 4:
        return -1
    try:
        ascii_value = encoded.encode("ascii")
    except UnicodeEncodeError:
        return -1
    if not re.fullmatch(rb"[A-Za-z0-9+/]*={0,2}", ascii_value):
        return -1
    padding = len(ascii_value) - len(ascii_value.rstrip(b"="))
    return (len(ascii_value) // 4) * 3 - padding


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
    def _uncertain_guidance(payload: dict[str, Any]) -> str:
        """Что осталось неизвестным и что с этим делать — словами, а не числом.

        Спека v3 §5 требует не только различать неизвестный исход, но и давать по
        нему указание («retry or reconciliation guidance»), а §«Users can see…» —
        показывать, «что осталось неизвестным и как это исправить».

        Прежняя редакция говорила «есть действия с НЕИЗВЕСТНЫМ исходом: 3»: число
        без имён не позволяет ни проверить, ни решить. Здесь называется САМО
        действие, потому что проверять человек будет именно его.

        Часть таких заявок система закрывает сама, наблюдением за состоянием
        (`_reconcile_uncertain_approvals`). Сюда доходит остаток — то, для чего
        проверки постусловия нет, и решить за человека, чем кончилось необратимое
        действие, нельзя.
        """
        raw = payload.get("items")
        items: list[Any] = raw if isinstance(raw, list) else []
        total = int(payload.get("total") or len(items))
        if not total:
            return ""
        lines = [
            "",
            f"⚠️ Действий с НЕИЗВЕСТНЫМ исходом: {total}.",
            "Их исполнение оборвалось на середине: эффект мог случиться, а мог и нет.",
        ]
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            what = str(item.get("summary") or item.get("tool") or "").strip()
            why = str(item.get("error") or "").strip()
            if what:
                lines.append(f"• {what[:160]}" + (f" — {why[:80]}" if why else ""))
        if total > 5:
            lines.append(f"…и ещё {total - 5}.")
        lines.append(
            "Проверьте по этим действиям сами, что получилось. Повторять их "
            "автоматически нельзя: повтор дал бы второй побочный эффект по одному решению."
        )
        return "\n".join(lines)

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

    async def _offer_access_to_owner(
        self,
        telegram: httpx.AsyncClient,
        actor: dict[str, Any],
        newcomer: dict[str, Any],
    ) -> None:
        """Сказать владельцу, что пришёл новичок, и дать кнопку выдать доступ.

        До этого `/start` обещал «владелец может расширить доступ», а механизма В
        ЧАТЕ не было вовсе: маршрут смены пресета существует, но владелец о
        новичке не узнавал ниоткуда, кроме админки. Обещание без исполнителя —
        это тот же мёртвый конец, что и кнопка без обработчика.

        Имя и фамилию из Telegram сюда не переносим: владельцу довольно того, что
        человек с таким именем написал, а лишние персональные поля в чужой чат
        отправлять незачем.
        """
        owner_chat = self._signer_chat_id()
        newcomer_id = str(actor.get("user_id") or actor.get("id") or "")
        if not owner_chat or not newcomer_id:
            return
        name = str(newcomer.get("first_name") or newcomer.get("username") or "новый человек")
        await self._send_message(
            telegram,
            int(owner_chat),
            f"Новый человек в чате: {name}. Сейчас у него режим новичка — "
            "чат, файлы и веб-поиск; миссии и выполнение кода закрыты.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Выдать полный доступ",
                            # Кнопка привязана к тому, кому её показали, — как у
                            # `conv` и `know`. Здесь это владелец: чат подписи и
                            # есть его учётка.
                            "callback_data": f"acc:grant:{newcomer_id}.{owner_chat}",
                        }
                    ]
                ]
            },
        )

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
            # Найдено состязательным ревью: Telegram доставляет каждый тик Live
            # Location как новый edited_message с ТЕМ ЖЕ message_id (обновляются
            # только координаты, ни text, ни caption не появляются вовсе — это не
            # правка написанного). Без проверки ниже это читалось как «человек
            # исправил текст» на каждый пинг геопозиции, раз в несколько секунд, и
            # бот заваливал чат одной и той же нерелевантной фразой.
            #
            # Правка сообщения С ТЕКСТОМ по-прежнему НЕ подхватывается: заметка уже
            # ушла в базу со старым текстом (идемпотентность держится за source_ref
            # обновления), и молчание означало бы, что в чате один текст, а в
            # архиве навсегда другой — владелец об этом не знает.
            if not (str(edited.get("text") or "").strip() or str(edited.get("caption") or "").strip()):
                return
            raw_chat = edited.get("chat")
            edited_chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
            edited_chat_id = int(edited_chat.get("id") or 0)
            # Тот же предикат, что у исходящих и у кнопок: самозарегистрированный
            # newcomer тоже правит свои сообщения, и молчание оставляло бы его с
            # текстом в чате, отличным от того, что лежит в архиве.
            if self._may_message_chat(edited_chat_id):
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
        album_value = update.get("friday_media_group_messages")
        album_messages: list[dict[str, Any]] = (
            [dict(item) for item in album_value if isinstance(item, dict)]
            if isinstance(album_value, list)
            else []
        )
        if album_value is not None and not album_messages:
            raise PermanentUpdateError("Telegram album has no usable parts")
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

        update_id = int(update.get("update_id") or -1)
        if update_id < 0:
            raise PermanentUpdateError("Telegram update has no valid identity")
        # The worker normally inserted this exact update before dispatch.  Keep
        # the private processing seam independently safe as well: every model
        # answer must have a durable delivery row before its first Telegram
        # byte, including recovery/replay callers which invoke this method
        # directly. ``store`` is INSERT-OR-IGNORE, so the worker-owned payload
        # and its retry state are never overwritten.
        self._inbox.store(update)
        archive_password = self._archive_passwords.get(update_id)
        pending_archive = self._inbox.archive_password_challenge(chat_id, int(external_user_id))
        password_followup = update.get("friday_archive_password_followup") is True
        if password_followup:
            if pending_archive is None:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Не вижу архива, который ждёт пароль. Пришлите архив ещё раз вместе с паролем.",
                )
                return
            if archive_password is None:
                # The durable update is intentionally password-free.  A restart
                # between intake and processing loses the ephemeral secret, so
                # ask again instead of guessing or sending the redacted marker.
                await self._send_message(
                    telegram,
                    chat_id,
                    "Пароль не сохранился после перезапуска. Пришлите его ещё раз.",
                )
                return
            message = dict(message)
            message.pop("text", None)
            message["caption"] = str(pending_archive.get("safe_query") or "")
            if pending_archive.get("reply_recovery") is True:
                # Keep the historical media as a structural reply pointer.  It
                # must pass through the same ABSENT -> bounded redownload ->
                # exact-byte recovery route as the original turn; presenting it
                # as current media would silently fall back to ordinary upload
                # deduplication after a password challenge.
                message.pop("document", None)
                message["reply_to_message"] = {
                    "message_id": int(pending_archive["reply_document_message_id"]),
                    "document": dict(pending_archive["document"]),
                }
            else:
                message["document"] = dict(pending_archive["document"])
            update["message"] = message
        elif (
            update.get("friday_archive_password_supplied") is True
            and self._archive_document_descriptor(message) is None
            and (
                not isinstance(message.get("reply_to_message"), dict)
                or self._archive_document_descriptor(message["reply_to_message"]) is None
            )
            and pending_archive is None
        ):
            await self._send_message(
                telegram,
                chat_id,
                "Не вижу архива, к которому относится пароль. Пришлите архив вместе с ним.",
            )
            return

        text = str(message.get("text") or message.get("caption") or "").strip()
        # В ГРУППЕ Пятница отвечает только когда обращаются к ней. Прежде в
        # разрешённой группе к модели уходило КАЖДОЕ сообщение: разговор двух
        # людей о своём становился и вопросом, и материалом для архива, и счётом
        # за модель. Обращением считаются три вещи и только они: упоминание по
        # `@имени`, ответ на сообщение самой Пятницы и команда.
        #
        # Личная переписка не меняется ничем: там обращение и есть само сообщение.
        addressed, text = (True, text) if password_followup else self._group_address(message, chat, text)
        if not addressed:
            return
        # Альбом Telegram шлёт несколькими сообщениями с общим `media_group_id`, и
        # подпись стоит ровно у одной части. Остальные приходили совсем пустыми:
        # «вот договор, пять страниц» относилось к одному файлу из пяти, а четыре
        # попадали в архив без единого слова о том, что это.
        #
        # Помощник зовётся ВСЕГДА, а не только при пустом тексте: у части С
        # подписью он её ЗАПОМИНАЕТ и возвращает пустую строку. Первая редакция
        # звала его под `if not text`, то есть ровно мимо той части, у которой
        # подпись есть, — и запоминать было нечего.
        borrowed_caption = self._album_caption(message)
        if not text:
            text = borrowed_caption

        # Правка записи: человек ответил репликой НА приглашение «пришлите новый
        # текст». Адресат однозначен даже в чате, где идёт несколько разговоров,
        # потому что цепляемся за идентификатор конкретного сообщения, а не за
        # «последнее действие». Ход при этом не идёт к модели вовсе: это правка,
        # а не вопрос.
        replied_to = message.get("reply_to_message")
        edit_target = ""
        if isinstance(replied_to, dict):
            edit_target = self._inbox.take_edit_prompt(int(replied_to.get("message_id") or 0))
        if edit_target and text:
            await self._backend_json(
                backend,
                "PATCH",
                f"/api/knowledge/{edit_target}",
                {"content": text, "telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                "Запись исправлена: теперь в ней ваш текст. Заголовок и связи прежние.",
            )
            return
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
                "Привет! Я Friday, по-русски — Пятница: локальная система личных знаний. Отправьте заметку, "
                "вопрос, изображение, документ, голосовое, аудио или видео, геолокацию или "
                "контакт. Аудио и видео сохраняются как есть (без расшифровки) и ждут вашего "
                "решения в Inbox. Спорные знания и связи останутся на ваше подтверждение.\n\n"
                "/help — команды"
            )
            raw_actor = me.get("actor")
            actor: dict[str, Any] = raw_actor if isinstance(raw_actor, dict) else {}
            newcomer = str(actor.get("preset_key") or "") == "newcomer"
            if newcomer:
                start_text = (
                    f"{start_text}\n\n"
                    "Сейчас у вас режим новичка: чат, файлы и веб-поиск доступны, "
                    "миссии и выполнение кода — нет. Я сказала владельцу, что вы пришли: "
                    "расширить доступ он может одной кнопкой."
                )
            # Сначала отвечаем ТОМУ, КТО ЖДЁТ. Владелец узнаёт следом: он не стоит
            # у экрана, а человек, написавший `/start`, стоит.
            await self._send_message(telegram, chat_id, start_text)
            if newcomer:
                # Обещание «владелец может расширить доступ» до этого не имело
                # механизма В ЧАТЕ вовсе: маршрут смены пресета существует, но
                # владелец о новичке не узнавал ниоткуда, кроме админки, — то есть
                # фраза была обещанием без исполнителя.
                await self._offer_access_to_owner(telegram, actor, user)
            return
        if command == "/help":
            await register_backend_user()
            engineer_help = (
                "/engineer — разбор файлов и аудит хостов владельца\n"
                if self.config.engineer_mode_enabled
                else ""
            )
            await self._send_message(
                telegram,
                chat_id,
                "Команды:\n"
                "/chat — обычный разговор\n"
                "/work — работа с личными знаниями\n"
                "/research — многошаговое исследование\n"
                f"{engineer_help}"
                "/mission цель — многошаговая миссия в фоне\n"
                "/missions — список миссий и управление\n"
                "/inbox — разобрать ближайшие предложения\n"
                "/conflicts — разобрать конфликты знаний (порциями)\n"
                "/relations — принять или отклонить предложенные связи (порциями)\n"
                "/merges — подтвердить или отклонить объединение дубликатов\n"
                "/tags — теги базы знаний с количеством записей\n"
                "/browse тег или название — записи по тегу, проекту или сущности\n"
                "/profile имя — карточка объекта: документы, теги, даты, связи\n"
                "/graph первый => второй — как связаны двое: цепочка связей\n"
                "/search запрос — найти записи по смыслу, без ответа модели\n"
                "/history запрос — найти реплики в истории переписки\n"
                "/status — состояние базы\n"
                "/why — почему был такой ответ\n"
                "/new — начать новый диалог\n"
                "/archive — архивировать текущий разговор\n"
                "/delete — убрать текущий разговор из списка (переписка сохраняется)\n"
                "/rename название — переименовать текущий разговор\n"
                "/note текст — явно сохранить заметку\n"
                "/instructions — как отвечать: показать, задать или очистить\n"
                "/retry — сгенерировать ответ на последний вопрос заново\n"
                "/reminders — предстоящие напоминания; кнопка «Снять» отменяет одно\n"
                "/export — скачать текущий разговор текстом\n"
                + (
                    "/obsidian — подключить или проверить Obsidian на Android\n"
                    "/obsidian_alias имя — задать имя Android-vault для ссылок\n"
                    if self.config.obsidian_enabled
                    else ""
                )
                + "\n"
                "Ответы можно оценивать кнопками, а результаты /work, /research и миссий — "
                "отправлять в Inbox на review.",
            )
            return
        if command == "/obsidian":
            if not self.config.obsidian_enabled:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Интеграция Obsidian не включена владельцем Friday.",
                )
                return
            # The panel contains a private per-user Device ID and pairing state.
            # Even an allowlisted group must never receive either one.
            if str(chat.get("type") or "") != "private" or external_user_id != str(chat_id):
                await self._send_message(
                    telegram,
                    chat_id,
                    "Настройка Obsidian доступна только в личной переписке со мной.",
                )
                return
            # `start` is idempotent and doubles as resume: reopening /obsidian
            # always asks the backend for the durable current state.
            response = await self._backend_json(
                backend,
                "POST",
                "/api/obsidian/onboarding/start",
                None,
                external_user_id,
                str(chat_id),
            )
            panel_text, panel_markup = obsidian_panel(response)
            await self._send_message(
                telegram,
                chat_id,
                panel_text,
                reply_markup=panel_markup,
            )
            return
        if command == "/obsidian_alias":
            if not self.config.obsidian_enabled:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Интеграция Obsidian не включена владельцем Friday.",
                )
                return
            if str(chat.get("type") or "") != "private" or external_user_id != str(chat_id):
                await self._send_message(
                    telegram,
                    chat_id,
                    "Настройка Obsidian доступна только в личной переписке со мной.",
                )
                return
            if not argument:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Укажите точное имя vault: /obsidian_alias Friday",
                )
                return
            response = await self._backend_json(
                backend,
                "POST",
                "/api/obsidian/onboarding/vault-alias",
                {"alias": argument},
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                "Имя vault для Obsidian-ссылок обновлено.",
            )
            panel_text, panel_markup = obsidian_panel(response)
            await self._send_message(
                telegram,
                chat_id,
                panel_text,
                reply_markup=panel_markup,
            )
            return
        if command == "/retry":
            await register_backend_user()
            typing_task = asyncio.create_task(self._typing_loop(telegram, chat_id))
            try:
                try:
                    response = await self._backend_json(
                        backend,
                        "POST",
                        "/api/me/regenerate",
                        {"operation_id": f"telegram-update:{update_id}"},
                        external_user_id,
                        str(chat_id),
                    )
                except PermanentUpdateError:
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Нечего повторять: в этом чате ещё не было вопроса. "
                        "Задайте вопрос — и /retry сгенерирует ответ заново.",
                    )
                    return
            finally:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            await self._send_message(
                telegram,
                chat_id,
                self._format_response_message(response),
                reply_markup=self._response_reply_markup(response, external_user_id=external_user_id),
                text_format=_response_text_format(response),
            )
            await self._deliver_voice_reply(telegram, chat_id, response)
            await self._deliver_generated_files(telegram, chat_id, response)
            return
        if command == "/instructions":
            me = await register_backend_user()
            if not argument:
                raw_metadata = (me.get("user") or {}).get("metadata_json") or ""
                try:
                    saved = str(json.loads(str(raw_metadata) or "{}").get("custom_instructions") or "")
                except (json.JSONDecodeError, TypeError):
                    saved = ""
                await self._send_message(
                    telegram,
                    chat_id,
                    f"Сейчас: {saved}"
                    if saved
                    else "Пожелание не задано. /instructions текст — задать, /instructions очистить — снять.",
                )
                return
            clean = "" if argument.casefold() in {"очистить", "сброс", "clear"} else argument
            await self._backend_json(
                backend,
                "PATCH",
                "/api/me/instructions",
                {"instructions": clean},
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                "Пожелание снято." if not clean else f"Принято: {clean}",
            )
            return
        if command in {"/chat", "/work", "/research", "/engineer", "/engeneer"}:
            mode = {
                "/chat": "dialogue",
                "/work": "knowledge_work",
                "/research": "research",
                "/engineer": "engineer",
                "/engeneer": "engineer",
            }[command]
            if mode == "engineer" and not self.config.engineer_mode_enabled:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Инженерный режим не включён в этом экземпляре Friday.",
                )
                return
            try:
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
            except PermanentUpdateError as error:
                if mode == "engineer" and getattr(error, "status_code", None) == 403:
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Инженерный режим доступен только владельцу.",
                    )
                    return
                raise
            labels = {
                "dialogue": "Обычный диалог",
                "knowledge_work": "Работа со знаниями",
                "research": "Исследование",
                "engineer": "Инженерный разбор",
            }
            extra = ""
            if str(data.get("mode")) == "engineer":
                extra = (
                    " Назовите хост, URL или киньте exe/apk — пойду сразу. "
                    "Координаты берёт из чата. Эксплойт-пейлоадов нет."
                )
            await self._send_message(
                telegram,
                chat_id,
                f"Режим: {labels.get(str(data.get('mode')), mode)}.{extra}",
            )
            return
        if command == "/inbox":
            await self._send_inbox(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/conflicts":
            await self._send_conflicts(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/relations":
            await self._send_relations(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/merges":
            await self._send_merges(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/reminders":
            await self._send_reminders(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/tags":
            await self._send_tags(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/compact":
            await self._send_compacts(telegram, backend, chat_id, external_user_id, user)
            return
        if command == "/browse":
            query = argument
            await self._send_browse(telegram, backend, chat_id, external_user_id, user, query)
            return
        if command == "/profile":
            await self._send_entity_profile(telegram, backend, chat_id, external_user_id, user, argument)
            return
        if command == "/graph":
            await self._send_relation_path(telegram, backend, chat_id, external_user_id, user, argument)
            return
        if command == "/watch":
            # Монитор — сохранённый вопрос, за которым система следит сама
            # (спека v3 §6). Условие это ТЕКСТ ЗАПРОСА: второй язык условий
            # означал бы вторую реализацию «что считается совпадением».
            if not argument.strip():
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /watch тема\n\n"
                    "Например: /watch поверка весов. Сообщу, когда появится новое по теме. "
                    "Список слежений: /watching",
                )
                return
            created = await self._backend_json(
                backend,
                "POST",
                "/api/me/monitors",
                {"query": argument.strip(), "telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            raw_monitor = created.get("monitor") if isinstance(created, dict) else None
            monitor: dict[str, Any] = raw_monitor if isinstance(raw_monitor, dict) else {}
            await self._send_message(
                telegram,
                chat_id,
                f"Слежу за темой «{monitor.get('query') or argument.strip()}». Сообщу, когда "
                "появится НОВОЕ по ней — то, что уже есть, показывать не буду: "
                f"для этого /search {argument.strip()}",
            )
            return
        if command == "/approvals":
            # Только в личной переписке. Список называет, ЧТО именно предлагается
            # сделать с личными данными («слить Иванова И.И. и Иванова Ивана»), и в
            # общей комнате это те же имена из чужого графа, которые уже однажды
            # пришлось убирать из проактивных сообщений. Кнопку чужой не нажмёт
            # (заявка привязана к владельцу, и чужому маршрут ответит 404), но
            # прочитать строку может каждый.
            if str(chat.get("type") or "") != "private":
                await self._send_message(
                    telegram,
                    chat_id,
                    "Подтверждения показываю только в личной переписке — напишите мне туда.",
                )
                return
            data = await self._backend_json(
                backend,
                "GET",
                "/api/me/approvals?status=pending",
                {"telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            items = data.get("items") if isinstance(data.get("items"), list) else []
            # Неизвестные исходы спрашиваются ВСЕГДА, а не только когда очередь
            # пуста. Прежняя редакция показывала их лишь в ветке «ничего не ждёт
            # решения», и одна ожидающая заявка полностью скрывала то, про что
            # система сама не знает, чем кончилось. Спека v3: человек видит, «что
            # осталось неизвестным и как это исправить».
            unknown = await self._backend_json(
                backend,
                "GET",
                "/api/me/approvals?status=uncertain",
                {"telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            tail = self._uncertain_guidance(unknown)
            if not items:
                await self._send_message(telegram, chat_id, "Ничего не ждёт вашего решения." + tail)
                return
            total = int(data.get("total") or len(items))
            await self._send_message(
                telegram,
                chat_id,
                f"Ждут вашего решения: {total}." if total else "Ждут вашего решения:",
            )
            shown = 0
            for index, item in enumerate(items[:10], start=1):
                if not isinstance(item, dict):
                    continue
                approval_id = str(item.get("id") or "")
                summary = str(item.get("summary") or item.get("tool") or "").strip()
                rows: list[list[dict[str, str]]] = []
                if approval_id and CALLBACK_TARGET_RE.fullmatch(approval_id):
                    rows.append(
                        [
                            {"text": f"✓ {index}", "callback_data": f"apr:yes:{approval_id}"},
                            {"text": f"✕ {index}", "callback_data": f"apr:no:{approval_id}"},
                        ]
                    )
                await self._send_message(
                    telegram,
                    chat_id,
                    f"{index}. {summary}",
                    reply_markup={"inline_keyboard": rows} if rows else None,
                )
                shown += 1
            footer = ""
            if total > shown:
                footer = f"Показаны первые {shown} из {total}."
            if footer or tail:
                await self._send_message(
                    telegram,
                    chat_id,
                    "\n".join(item for item in (footer, tail.strip()) if item),
                )
            return
        if command == "/watching":
            data = await self._backend_json(
                backend,
                "GET",
                "/api/me/monitors",
                {"telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                await self._send_message(telegram, chat_id, "Пока ни за чем не слежу. Начать: /watch тема")
                return
            lines = [f"Слежу за темами: {len(items)}."]
            stop_buttons: list[dict[str, str]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                monitor_id = str(item.get("id") or "")
                reported = int(item.get("matches_reported") or 0)
                lines.append(
                    f"• {str(item.get('query') or '')[:80]}"
                    + (f" — сообщений: {reported}" if reported else "")
                )
                if monitor_id and CALLBACK_TARGET_RE.fullmatch(monitor_id):
                    stop_buttons.append(
                        {
                            "text": f"✕ {len(stop_buttons) + 1}",
                            "callback_data": f"mon:stop:{monitor_id}",
                        }
                    )
            rows = [stop_buttons[index : index + 4] for index in range(0, len(stop_buttons), 4)]
            await self._send_message(
                telegram,
                chat_id,
                "\n".join([*lines, "", "Кнопкой ниже — снять слежение по номеру."]),
                reply_markup={"inline_keyboard": rows} if rows else None,
            )
            return
        if command == "/entity_alias":
            # Псевдоним — безопасная альтернатива слиянию, и на этом корпусе она
            # важнее самого слияния: «Иванов И.И.» и «Иванов Иван Иванович» могут
            # быть одним человеком, а могут и разными, и цена ошибки
            # несимметрична — два дубликата это неудобство, а два разных
            # человека в одном узле порча данных. Псевдоним чинит поиск, ничего
            # не соединяя: узлы остаются разными.
            entity_name, alias = _split_rename(argument)
            if not entity_name or not alias:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /entity_alias объект => псевдоним\n\n"
                    "Например: /entity_alias Иванов Иван Иванович => Иванов И.И.\n"
                    "Псевдоним помогает найти объект по другому написанию и НИЧЕГО не сливает.",
                )
                return
            try:
                found = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/kg/entity-profile?name={quote(entity_name, safe='')}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError as error:
                notice = refusal_notice(error)
                await self._send_message(
                    telegram,
                    chat_id,
                    notice or f"Объект «{entity_name}» не найден. Карточка: /profile {entity_name}",
                )
                return
            raw_found = found.get("entity") if isinstance(found, dict) else None
            found_entity: dict[str, Any] = raw_found if isinstance(raw_found, dict) else {}
            entity_id = str(found_entity.get("id") or "")
            if not entity_id:
                await self._send_message(telegram, chat_id, f"Объект «{entity_name}» не найден.")
                return
            # Псевдоним, который УЖЕ является именем другого объекта, — это не
            # псевдоним, а заявка на слияние. Команда обещает «ничего не сливает»,
            # и обещание надо держать: два разных человека под одним узлом это
            # порча данных, а слияние делается отдельным решением в /merges.
            try:
                clash = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/kg/entity-profile?name={quote(alias, safe='')}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError as error:
                # 404 здесь — законный ответ: такого объекта нет, столкновения нет.
                # Любой другой статус означает, что проверить НЕ УДАЛОСЬ, и пустой
                # словарь молча отключал бы сторожа: команда обещает «узлы не
                # слиты», а на деле дописала бы псевдоним, не посмотрев.
                if getattr(error, "status_code", None) != 404:
                    await self._send_message(
                        telegram,
                        chat_id,
                        refusal_notice(error)
                        or "Не удалось проверить, не занято ли это написание другим объектом. "
                        "Псевдоним не добавлен — повторите позже.",
                    )
                    return
                clash = {}
            raw_clash = clash.get("entity") if isinstance(clash, dict) else None
            clash_entity: dict[str, Any] = raw_clash if isinstance(raw_clash, dict) else {}
            clash_id = str(clash_entity.get("id") or "")
            if clash_id and clash_id != entity_id:
                await self._send_message(
                    telegram,
                    chat_id,
                    f"«{alias}» — это уже отдельный объект, а не написание для «{entity_name}». "
                    "Псевдонимом его сделать нельзя: это было бы скрытым слиянием двух узлов. "
                    "Если это действительно один и тот же — объедините их в /merges, "
                    f"а посмотреть второй: /profile {alias}",
                )
                return
            existing_aliases = found_entity.get("aliases_json")
            if isinstance(existing_aliases, str):
                try:
                    existing_aliases = json.loads(existing_aliases or "[]")
                except json.JSONDecodeError:
                    existing_aliases = []
            aliases = [str(item) for item in (existing_aliases or []) if str(item).strip()]
            # Сравнение нормализованное: у объекта уже есть «Иванов И.И.», человек
            # пишет «Иванов И. И.» — это то же самое написание, и второй записи
            # быть не должно. Все потребители псевдонимов сравнивают именно так.
            from friday.storage._base import normalize_entity_name

            known = {normalize_entity_name(item) for item in [*aliases, entity_name]}
            if normalize_entity_name(alias) in known:
                await self._send_message(
                    telegram, chat_id, f"У объекта «{entity_name}» уже есть такой псевдоним."
                )
                return
            await self._backend_json(
                backend,
                "PATCH",
                f"/api/kg/entities/{entity_id}",
                {"aliases": [*aliases, alias], "telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            await self._send_message(
                telegram,
                chat_id,
                f"«{alias}» теперь тоже находит объект «{entity_name}». Узлы не слиты — "
                "это другое действие, оно в /merges.",
            )
            return
        if command == "/entity_rename":
            # Переименование — единственное действие над объектом, которое нельзя
            # сделать кнопкой: новое имя надо ввести. Формат «старое => новое»
            # выбран потому, что имена содержат пробелы (ФИО, «в/ч 12345»), и
            # разделить их по пробелу нельзя.
            old_name, new_name = _split_rename(argument)
            if not old_name or not new_name:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /entity_rename старое имя => новое имя\n\n"
                    "Например: /entity_rename Иванов И.И. => Иванов Иван Иванович",
                )
                return
            try:
                found = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/kg/entity-profile?name={quote(old_name, safe='')}",
                    {"telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError as error:
                notice = refusal_notice(error)
                await self._send_message(
                    telegram,
                    chat_id,
                    notice or f"Объект «{old_name}» не найден. Карточка: /profile {old_name}",
                )
                return
            raw_found = found.get("entity") if isinstance(found, dict) else None
            renamed_target: dict[str, Any] = raw_found if isinstance(raw_found, dict) else {}
            entity_id = str(renamed_target.get("id") or "")
            if not entity_id:
                await self._send_message(telegram, chat_id, f"Объект «{old_name}» не найден.")
                return
            renamed = await self._backend_json(
                backend,
                "PATCH",
                f"/api/kg/entities/{entity_id}",
                {"name": new_name, "telegram_user": user},
                external_user_id,
                str(chat_id),
            )
            raw_renamed = renamed.get("entity") if isinstance(renamed, dict) else None
            renamed_entity: dict[str, Any] = raw_renamed if isinstance(raw_renamed, dict) else {}
            shown_name = str(renamed_entity.get("name") or new_name)
            await self._send_message(
                telegram,
                chat_id,
                f"Объект переименован: «{shown_name}». "
                f"Правку можно отменить в карточке: /profile {shown_name}",
            )
            return
        if command == "/search":
            query = argument
            await self._send_search(telegram, backend, chat_id, external_user_id, user, query)
            return
        if command == "/history":
            query = argument
            await self._send_history(telegram, backend, chat_id, external_user_id, user, query)
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
                # Просим ровно столько, сколько покажем: «сколько всего за период»
                # приходит отдельным полем `total`, посчитанным SQL без потолка.
                # Прежде здесь просили на один больше и печатали длину полученного
                # списка — из-за чего на марте с четырьмя сотнями документов человек
                # читал «показаны первые 10 из 11».
                f"/api/knowledge/by-date?since={since}&until={until}&limit={_TIMELINE_SHOWN}",
                None,
                external_user_id,
                str(chat_id),
            )
            events = await self._backend_json(
                backend,
                "GET",
                f"/api/kg/timeline?start={since}&end={until}&limit={_TIMELINE_SHOWN}",
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
        if command == "/export":
            # G20: plain-text transcript of the current channel conversation as a file.
            # Backend builds the body (tenant + truncation notice); bridge only ships it.
            await register_backend_user()
            try:
                text = await self._backend_text(
                    backend,
                    "GET",
                    "/api/conversations/current/export",
                    None,
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Нечего выгружать: в этом чате ещё нет разговора.",
                )
                return
            content = text.encode("utf-8")
            await self._send_document(
                telegram,
                chat_id,
                "jericho-export.txt",
                content,
                caption="Выгрузка текущего разговора (текст).",
            )
            return
        if command == "/archive":
            # G18a: backend already had POST /archive (conversations.manage); Telegram
            # never called it. Sentinel `current` resolves to this chat's session.
            await register_backend_user()
            try:
                data = await self._backend_json(
                    backend,
                    "POST",
                    "/api/conversations/current/archive",
                    {"archived": True, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Нечего архивировать: в этом чате ещё нет разговора.",
                )
                return
            raw_conv = data.get("conversation") if isinstance(data, dict) else None
            conv: dict[str, Any] = raw_conv if isinstance(raw_conv, dict) else {}
            title = str(conv.get("title") or "").strip() or "без названия"
            await self._send_message(
                telegram,
                chat_id,
                f"Разговор «{title}» архивирован. /new — начать новый; база знаний не тронута.",
            )
            return
        if command == "/delete":
            # G18b: hard delete — ask first (Да/Нет), same button pattern as /merges.
            #
            # The invoker's own id rides inside target_id ("current.{id}"), not
            # just the "current" sentinel: this prompt is visible to the whole
            # chat, and in any chat with more than one capable account (an
            # allowlisted group, or open registration admitting a second
            # person) a DIFFERENT member tapping "Да, удалить" used to delete
            # THEIR OWN current conversation instead — the button carried no
            # record of who it was shown for, so the callback handler had
            # nothing to check the presser against. Found by adversarial
            # review, confirmed live-reachable now that open registration is
            # on. `CALLBACK_TARGET_RE` allows `.`, so this needs no new
            # separator or callback-data version bump.
            await register_backend_user()
            await self._send_message(
                telegram,
                chat_id,
                "Удалить текущий разговор безвозвратно? Сообщения и оценки ответов "
                "пропадут; база знаний не тронута.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Да, убрать",
                                "callback_data": f"conv:delete:current.{external_user_id}",
                            },
                            {
                                "text": "Нет",
                                "callback_data": f"conv:keep:current.{external_user_id}",
                            },
                        ]
                    ]
                },
            )
            return
        if command == "/rename":
            # G18c: storage + PATCH are new; Telegram is just the self-service front.
            title = argument
            if not title:
                await register_backend_user()
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /rename новое название разговора",
                )
                return
            try:
                data = await self._backend_json(
                    backend,
                    "PATCH",
                    "/api/conversations/current",
                    {"title": title, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            except PermanentUpdateError:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Нечего переименовывать: в этом чате ещё нет разговора.",
                )
                return
            raw_conv = data.get("conversation") if isinstance(data, dict) else None
            conv = raw_conv if isinstance(raw_conv, dict) else {}
            new_title = str(conv.get("title") or title).strip()
            await self._send_message(telegram, chat_id, f"Разговор переименован: «{new_title}».")
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
            # Признак «думаю» стоял только у обычного хода и у /retry. Создание
            # миссии разбивает цель на шаги моделью, то есть длится столько же, —
            # а чат при этом молчал, и человек не понимал, дошла ли команда.
            mission_typing = asyncio.create_task(self._typing_loop(telegram, chat_id))
            try:
                created = await self._backend_json(
                    backend,
                    "POST",
                    "/api/missions",
                    {"goal": goal, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
            finally:
                mission_typing.cancel()
                await asyncio.gather(mission_typing, return_exceptions=True)
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
                "engineer": "инженерный разбор",
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
            response_to_send = cached_response
            if album_messages:
                expected_album_ids = [int(item.get("message_id") or 0) for item in album_messages]
                if cached_response.get(_ALBUM_CACHE_MESSAGE_IDS) != expected_album_ids:
                    raise PermanentUpdateError(
                        "Cached Telegram response does not belong to the complete album"
                    )
                response_to_send = dict(cached_response)
                response_to_send.pop(_ALBUM_CACHE_MESSAGE_IDS, None)
            await self._send_message(
                telegram,
                chat_id,
                self._format_response_message(response_to_send),
                reply_markup=self._response_reply_markup(response_to_send, external_user_id=external_user_id),
                text_format=_response_text_format(response_to_send),
                # Повтор после обрыва: куски, уже дошедшие до человека, не уходят
                # второй раз. Текст тот же самый — он взят из кеша, не из модели.
                resume_key=int(update["update_id"]),
                reply_source_message_id=str(response_to_send.get("message_id") or ""),
                reply_to_message_id=message.get("message_id"),
            )
            await self._deliver_voice_reply(telegram, chat_id, response_to_send)
            await self._deliver_generated_files(telegram, chat_id, response_to_send)
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

        prepared_documents: list[dict[str, Any]] = []
        album_skipped_message_ids: set[int] = set()
        for media_message in album_messages or [message]:
            media_message_id = media_message.get("message_id")
            album_message_id = (
                media_message_id
                if isinstance(media_message_id, int) and not isinstance(media_message_id, bool)
                else None
            )
            if album_messages and album_message_id is None:
                raise RuntimeError("Telegram album has invalid ordered message identity")
            if album_messages and self._archive_document_descriptor(media_message) is not None:
                assert album_message_id is not None
                album_skipped_message_ids.add(album_message_id)
                continue
            try:
                prepared = await self._prepare_document(telegram, media_message, update)
            except (MediaTooLargeError, PermanentUpdateError) as exc:
                if album_messages:
                    # One permanently invalid/oversized sibling is an explicit
                    # terminal item, not authority to discard every valid row.
                    # A single bounded warning is composed with the final album
                    # response below; no second user-visible answer is sent.
                    assert album_message_id is not None
                    album_skipped_message_ids.add(album_message_id)
                    continue
                if isinstance(exc, MediaTooLargeError):
                    await register_backend_user()
                    await self._send_message(
                        telegram,
                        chat_id,
                        str(exc)
                        or "Файл слишком большой — Telegram-медиа превышает допустимый размер и не сохранено.",
                    )
                    return
                raise
            if prepared is None:
                if album_messages:
                    assert album_message_id is not None
                    album_skipped_message_ids.add(album_message_id)
                continue
            if isinstance(media_message_id, int) and not isinstance(media_message_id, bool):
                prepared["telegram_message_id"] = media_message_id
            prepared_documents.append(prepared)
        documents = prepared_documents if album_messages else []
        document = prepared_documents[0] if prepared_documents and not album_messages else None

        if not album_messages and not text and not document and not documents:
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
        replied_to = message.get("reply_to_message")
        replied_message_id = int(replied_to.get("message_id") or 0) if isinstance(replied_to, dict) else 0
        reply_source_message_id = (
            self._inbox.outbound_reply_source_message_id(chat_id, replied_message_id)
            if replied_message_id > 0
            else ""
        )
        if reply_source_message_id and not album_messages:
            # Opaque backend identity only.  No quoted text, filename, Raw id or
            # attachment metadata becomes authority at the transport boundary.
            # Albums are staged before their one model turn.  Preserve the old
            # document-turn contract where an incidental quoted assistant reply
            # cannot become a second attachment-authority lane merely because
            # the final request carries stage ids instead of raw bytes.
            payload["reply_source_message_id"] = reply_source_message_id
        if forward:
            payload["forward"] = forward
        if documents:
            payload["documents"] = documents
        elif document:
            payload["document"] = document
        else:
            # A reply to an earlier supported file is a pointer, not a new upload.
            # Do not call getFile/download and never let it compete with media on
            # the current turn; the backend resolves this exact opaque provenance.
            reply_document_source_ref = self._reply_document_source_ref(message)
            if reply_document_source_ref:
                payload["reply_document_source_ref"] = reply_document_source_ref
                if replied_message_id > 0:
                    # The backend combines this code-owned structural id with
                    # the authenticated bridge chat id.  Neither quoted text nor
                    # a filename participates in the resulting authority.
                    payload["reply_document_message_id"] = replied_message_id
                unique_id = _reply_document_file_unique_id(message)
                if unique_id:
                    payload["reply_document_file_unique_id"] = unique_id
        if archive_password is not None:
            payload["archive_password"] = archive_password
        if album_messages:
            # Old album attempts used the anchor's single-update key.  A
            # COMPLETE/uncertain legacy row must never replay over, or conflict
            # with, the v2 staged turn.  Bind the new namespace to every
            # ordered message identity and its prepared per-file source digest.
            payload["source_ref"] = _album_final_source_ref(
                int(update["update_id"]), album_messages, prepared_documents
            )
        # На что человек ответил репликой. Прежде `reply_to_message` не читался
        # вовсе: человек отвечал на конкретное сообщение — своё или Пятницы, — и
        # связь терялась. Отдельным полем, а не приклеенным к тексту: текст хода
        # идёт в архив как слова человека и в классификатор графа.
        quoted = self._reply_quote(message)
        if quoted:
            payload["reply_to"] = quoted
        progress_state = _ChatProgressState(text)
        typing_task = asyncio.create_task(self._typing_loop(telegram, chat_id))
        try:
            album_stage_receipts: list[dict[str, Any]] = []
            ready_documents: list[dict[str, Any]] = []

            async def stage_chunk(chunk: list[dict[str, Any]]) -> None:
                stage_payload = {
                    "message": "",
                    "documents": chunk,
                    "document_stage_only": True,
                    "telegram_user": user,
                }
                try:
                    stage_response = await self._backend_json(
                        backend,
                        "POST",
                        "/api/chat",
                        stage_payload,
                        external_user_id,
                        str(chat_id),
                    )
                except PermanentUpdateError as exc:
                    if exc.status_code not in {400, 409, 413, 422}:
                        raise
                    if len(chunk) > 1:
                        boundary = len(chunk) // 2
                        await stage_chunk(chunk[:boundary])
                        await stage_chunk(chunk[boundary:])
                        return
                    if exc.status_code == 409:
                        # A singleton conflict can be a post-persist receipt
                        # seam, not proof that this sibling is invalid.  Keep
                        # the owned album retryable; successful siblings replay
                        # by their exact source refs on the next attempt.
                        raise RuntimeError("Telegram album stage is not yet converged") from exc
                    album_skipped_message_ids.add(int(chunk[0]["telegram_message_id"]))
                    return

                receipts = stage_response.get("file_ingestions")
                if not isinstance(receipts, list) or len(receipts) != len(chunk):
                    raise RuntimeError("Backend returned an incomplete Telegram album stage")
                for staged_document, item in zip(chunk, receipts, strict=True):
                    expected = _telegram_item_receipt(staged_document)
                    actual = item.get("telegram_item_receipt") if isinstance(item, dict) else None
                    ready = item.get("telegram_stage_ready") if isinstance(item, dict) else None
                    if (
                        expected is None
                        or not isinstance(actual, dict)
                        or set(actual) != {"telegram_message_id", "source_ref_sha256"}
                        or actual != expected
                        or type(ready) is not bool
                    ):
                        raise RuntimeError("Backend returned an invalid Telegram album stage receipt")
                    if not ready:
                        album_skipped_message_ids.add(int(staged_document["telegram_message_id"]))
                        continue
                    ready_documents.append(staged_document)
                    album_stage_receipts.append({"telegram_item_receipt": actual})

            if album_messages:
                # The backend bounds one document turn by total decoded bytes.
                # Stage deterministic chunks under that same finite ceiling;
                # a permanent aggregate rejection is bisected to singleton so
                # one bad item never discards healthy siblings.
                chunks: list[list[dict[str, Any]]] = []
                current_chunk: list[dict[str, Any]] = []
                current_bytes = 0
                byte_limit = max(1, int(self.config.max_document_bytes))
                for staged_document in documents:
                    decoded_size = _prepared_document_size(staged_document)
                    if decoded_size < 0 or decoded_size > byte_limit:
                        album_skipped_message_ids.add(int(staged_document["telegram_message_id"]))
                        continue
                    if current_chunk and current_bytes + decoded_size > byte_limit:
                        chunks.append(current_chunk)
                        current_chunk = []
                        current_bytes = 0
                    current_chunk.append(staged_document)
                    current_bytes += decoded_size
                if current_chunk:
                    chunks.append(current_chunk)
                for chunk in chunks:
                    await stage_chunk(chunk)

                expected_message_ids = [int(item["message_id"]) for item in album_messages]
                ready_message_ids = [int(item["telegram_message_id"]) for item in ready_documents]
                if (
                    len(set(ready_message_ids).union(album_skipped_message_ids)) != len(expected_message_ids)
                    or set(ready_message_ids).intersection(album_skipped_message_ids)
                    or set(expected_message_ids) != set(ready_message_ids).union(album_skipped_message_ids)
                ):
                    raise RuntimeError("Telegram album staging did not terminate every ordered sibling")
                payload.pop("documents", None)
                if ready_message_ids:
                    payload["staged_document_message_ids"] = ready_message_ids

            if album_messages and not ready_documents:
                response = {
                    "message": (
                        "Не удалось принять ни одного файла из альбома. "
                        f"Отклонено: {len(album_skipped_message_ids)}. "
                        "Пришлите поддерживаемые файлы меньшего размера отдельными сообщениями."
                    ),
                    "message_format": "plain",
                }
            else:
                response = await _final_chat_request_with_progress(
                    self,
                    telegram,
                    backend,
                    payload,
                    external_user_id,
                    chat_id,
                    message.get("message_id"),
                    progress_state,
                )
            if album_messages:
                response["file_ingestions"] = album_stage_receipts
                if album_skipped_message_ids and ready_documents:
                    response = dict(response)
                    response["message"] = (
                        f"⚠️ Не удалось принять {len(album_skipped_message_ids)} из "
                        f"{len(album_messages)} файлов альбома; остальные обработаны.\n\n"
                        f"{str(response.get('message') or '').strip()}"
                    ).strip()
            if response.get("reply_media_recovery_required") is True:
                if document is not None or documents or not isinstance(replied_to, dict):
                    raise PermanentUpdateError("Backend requested invalid reply media recovery")
                try:
                    recovered_document = await self._prepare_document(
                        telegram,
                        replied_to,
                        {"update_id": replied_message_id or int(update["update_id"])},
                    )
                except MediaTooLargeError as exc:
                    await register_backend_user()
                    await self._send_message(
                        telegram,
                        chat_id,
                        str(exc)
                        or "Файл слишком большой — Telegram-медиа превышает допустимый размер и не сохранено.",
                    )
                    return
                if recovered_document is None:
                    raise PermanentUpdateError("Replied Telegram media is unavailable")
                payload["reply_document_recovery"] = recovered_document
                response = await _final_chat_request_with_progress(
                    self,
                    telegram,
                    backend,
                    payload,
                    external_user_id,
                    chat_id,
                    message.get("message_id"),
                    progress_state,
                )
                if response.get("reply_media_recovery_required") is True:
                    raise PermanentUpdateError("Backend did not accept reply media recovery")
            password_challenge = bool(
                response.get("archive_password_required") is True
                or response.get("archive_password_invalid") is True
            )
            archive_descriptor = self._archive_document_descriptor(message)
            if archive_descriptor is None and "reply_document_recovery" in payload:
                archive_descriptor = (
                    self._archive_document_descriptor(replied_to) if isinstance(replied_to, dict) else None
                )
            if password_challenge and archive_descriptor is not None:
                challenge_descriptor = dict(archive_descriptor)
                if "reply_document_recovery" in payload:
                    challenge_descriptor.update(
                        {
                            "_friday_reply_recovery": True,
                            "_friday_reply_document_source_ref": payload.get("reply_document_source_ref"),
                            "_friday_reply_document_message_id": payload.get("reply_document_message_id"),
                            "_friday_reply_document_file_unique_id": payload.get(
                                "reply_document_file_unique_id", ""
                            ),
                        }
                    )
                self._inbox.remember_archive_password_challenge(
                    chat_id,
                    int(external_user_id),
                    challenge_descriptor,
                    safe_query=text,
                    # An invalid retry must not move the challenge origin to the
                    # password message.  Keeping the original request stable is
                    # what lets later replies and diagnostics identify the same
                    # archive rather than a chain of failed credentials.
                    original_message_id=(
                        int(pending_archive.get("original_message_id") or 0)
                        if pending_archive is not None and password_followup
                        else int(message.get("message_id") or 0)
                    ),
                )
            elif archive_descriptor is not None or pending_archive is not None:
                self._inbox.clear_archive_password_challenge(chat_id, int(external_user_id))
            response_to_cache = dict(response)
            if album_messages:
                response_to_cache[_ALBUM_CACHE_MESSAGE_IDS] = [
                    int(item.get("message_id") or 0) for item in album_messages
                ]
            self._inbox.cache_backend_response(int(update["update_id"]), response_to_cache)
        except PermanentUpdateError as exc:
            if album_messages and exc.status_code == 409:
                # A 409 after a persistent backend seam is not proof that every
                # sibling is invalid. Keep the whole owned group retryable; its
                # stable per-file source refs make already-received siblings
                # replay-only while missing siblings continue.
                raise RuntimeError("Telegram album backend state is not yet converged") from exc
            raise
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
        await self._send_message(
            telegram,
            chat_id,
            self._format_response_message(response),
            reply_markup=self._response_reply_markup(response, external_user_id=external_user_id),
            text_format=_response_text_format(response),
            # Ответ уже в кеше строки очереди (строкой выше), поэтому повтор после
            # обрыва разрежет тот же текст и продолжит с места обрыва.
            resume_key=int(update["update_id"]),
            reply_source_message_id=str(response.get("message_id") or ""),
            reply_to_message_id=message.get("message_id"),
        )
        await self._deliver_voice_reply(telegram, chat_id, response)
        await self._deliver_generated_files(telegram, chat_id, response)
