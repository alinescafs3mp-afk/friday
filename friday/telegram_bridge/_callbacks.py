"""Telegram bridge: routing an inline-button press back to its action.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import binascii
import hashlib
import ipaddress
import re
import unicodedata
from contextlib import suppress
from urllib.parse import urlunsplit

from friday.telegram_bridge._base import (
    CALLBACK_TARGET_RE,
    LOGGER,
    Any,
    BridgeShared,
    PermanentUpdateError,
    base64,
    httpx,
    quote,
    refusal_notice,
    urlsplit,
)
from friday.telegram_bridge._obsidian import obsidian_panel

_FILE_PROMOTED_STATUS = "✅ Файл стал знанием — можно спрашивать."
_FILE_REVIEW_STATUS = "📥 Файл ждёт разбора в /inbox — в поиск попадёт после подтверждения."
_FILE_TRANSIENT_STATUS = "📄 Файл разобран, но по вашей просьбе НЕ сохранён."
_VOICE_UNRECOGNISED_CHAT_WARNING = (
    "🎤 Голос не распознался — слов в записи не разобрала. "
    "Повторите текстом или наговорите ещё раз поближе к микрофону."
)
_LEGACY_NUMERIC_IPV4 = re.compile(
    r"(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)){0,3}",
    re.IGNORECASE,
)
_PRIVATE_DNS_SUFFIXES = (
    ".alt",
    ".corp",
    ".example",
    ".home",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)
_UNOWNED_AUTOLINK = re.compile(
    r"(?<![`@\w])(?:"
    r"(?:www\.)?(?:[\w](?:[\w-]{0,61}[\w])?\.)+[\w-]{2,63}|"
    r"(?:\d{1,3}\.){3}\d{1,3}|"
    r"\[[0-9A-Fa-f:]{2,45}\]"
    r")(?::\d{1,5})?(?:[/?#][^\s<>\[\]{}()]*)?(?![`\w])",
    re.IGNORECASE,
)


def _neutralize_unowned_autolinks(text: str) -> str:
    """Keep dotted facts visible while preventing a second clickable source."""

    return _UNOWNED_AUTOLINK.sub(lambda match: f"`{match.group(0)}`", str(text or ""))


def _canonical_web_source_identity(parsed: Any, hostname: str, port: int | None) -> str:
    """Normalize aliases before the five-source transport limit is applied."""

    unreserved = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    def normalize_component(value: str) -> str:
        return re.sub(
            r"%([0-9A-Fa-f]{2})",
            lambda match: (
                decoded
                if (decoded := chr(int(match.group(1), 16))) in unreserved
                else f"%{match.group(1).upper()}"
            ),
            value,
        )

    def remove_dot_segments(path: str) -> str:
        absolute = path.startswith("/")
        trailing = path.endswith(("/.", "/.."))
        output: list[str] = []
        for segment in path.split("/"):
            if segment == ".":
                continue
            if segment == "..":
                if output and output[-1] != ".." and not (absolute and len(output) == 1 and output[0] == ""):
                    output.pop()
                elif not absolute:
                    output.append(segment)
                continue
            output.append(segment)
        result = "/".join(output)
        if absolute and not result.startswith("/"):
            result = f"/{result}"
        if absolute and not result:
            result = "/"
        if trailing and result != "/" and not result.endswith("/"):
            result = f"{result}/"
        return result

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    raw_path = normalize_component(parsed.path or "/")
    path = remove_dot_segments(raw_path)
    return urlunsplit((parsed.scheme.casefold(), netloc, path, normalize_component(parsed.query), ""))


def _web_source_chat_lines(value: Any) -> list[str]:
    """Validate the backend's bounded source ledger at the last transport hop."""

    if not isinstance(value, list):
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if (
            not url
            or len(url) > 2_048
            or any(
                char.isspace() or ord(char) == 127 or unicodedata.category(char).startswith("C")
                for char in url
            )
            or "\\" in url
        ):
            continue
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        raw_hostname = parsed.hostname.rstrip(".").casefold()
        if not raw_hostname or "%" in raw_hostname:
            continue
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").rstrip(".").casefold()
        except UnicodeError:
            continue
        if (
            not hostname
            or hostname in {"home.arpa", "localhost", "localhost.localdomain"}
            or hostname.endswith(_PRIVATE_DNS_SUFFIXES)
        ):
            continue
        try:
            address = ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            if _LEGACY_NUMERIC_IPV4.fullmatch(hostname) or "." not in hostname:
                continue
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                continue
        identity = _canonical_web_source_identity(parsed, hostname, port)
        if identity in seen:
            continue
        seen.add(identity)
        display_netloc = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            display_netloc = f"{display_netloc}:{port}"
        url = urlunsplit(
            (
                parsed.scheme.casefold(),
                display_netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        raw_title = str(item.get("title") or "")
        title = "" if any(unicodedata.category(char).startswith("C") for char in raw_title) else raw_title
        title = " ".join(title.split())[:120]
        # This function returns Markdown input.  A source title is a plain label,
        # never a second formatting surface controlled by a web page.
        for token in "[]()*_~`<>":
            title = title.replace(token, " ")
        title = " ".join(title.split())
        # The whole destination is stashed by the Markdown renderer before any
        # emphasis pass.  Encode only delimiter bytes which could change link
        # balancing; ordinary official URLs with underscores remain intact.
        destination = (
            url.replace("`", "%60")
            .replace("[", "%5B")
            .replace("]", "%5D")
            .replace("(", "%28")
            .replace(")", "%29")
        )
        # A page-controlled title must never disguise the destination host
        # ("Central Bank" pointing at an unrelated domain).  Keep the useful
        # title, but always expose the canonical host in the clickable label.
        label = f"{title} — {hostname}" if title else hostname
        lines.append(f"- [{label}]({destination})")
    return lines


def _file_fate_line(file_ingestion: Any) -> str:
    """Одна строка о судьбе присланного файла — стал знанием или ждёт разбора.

    Бэкенд честно возвращал `file_ingestion` с каждым файлом, и ни бридж, ни
    модель его не читали: владелец отправлял документ и не знал, попал тот в
    знания или завис в Inbox — а это разные следующие шаги (спрашивать можно
    сразу или сначала подтвердить в /inbox).
    """
    if not isinstance(file_ingestion, dict):
        return ""
    # Приёмный путь кладёт исход разбора ВЛОЖЕННЫМ словарём `extraction`, а
    # верхнеуровневый `extraction_success` производит осмотр без сохранения
    # (`inspect_file_transient`). Ниже обе транспортные формы сводятся до выбора
    # lifecycle-статуса. Раньше предупреждение «текст извлечь не удалось» было
    # физически недостижимо:
    # проверено прогоном настоящего `ingest_file` на .png и на .ogg. Человек
    # присылал картинку или наговаривал вопрос, который не расслышали, и получал
    # «📥 Файл ждёт разбора в /inbox» — ни слова о том, что содержимого не видно.
    if file_ingestion.get("voice_unrecognised"):
        return (
            "🎤 Голос не распознался — я сохранила запись, но слов в ней не разобрала. "
            "Повторите текстом или наговорите ещё раз поближе к микрофону."
        )
    nested_extraction = file_ingestion.get("extraction")
    extraction = dict(nested_extraction) if isinstance(nested_extraction, dict) else {}
    # No-save inspection returns these facts flat.  Normal ingestion nests them
    # under ``extraction``.  Merge only the same bounded public fields so both
    # transport shapes produce one truthful warning without exposing parser
    # diagnostics or content.
    for key in (
        "text_truncated",
        "parse_deadline_reached",
        "parse_pages_truncated",
        "parse_pages_read",
        "parse_total_pages",
        "vision_pages_read",
        "vision_pages_total",
        "archive_truncated",
        "archive_files",
        "archive_files_read",
        "source_truncated_for_parse",
        "unsupported_format",
    ):
        if key not in extraction and key in file_ingestion:
            extraction[key] = file_ingestion[key]
    parser_failed = file_ingestion.get("extraction_success") is False or extraction.get("success") is False
    parser_succeeded = file_ingestion.get("extraction_success") is True or extraction.get("success") is True
    # `text_success=False` answers whether any text came out, not whether the
    # parser failed.  A genuinely empty DOCX therefore has the truthful shape
    # `success=True, text_success=False, chars=0`.  Treat it as empty only when
    # parse success (or the public projection's explicit `empty_text`) proves
    # that state; an explicit parser failure wins over contradictory metadata.
    explicitly_empty = file_ingestion.get("empty_text") is True
    nothing_came_out = not parser_failed and (
        explicitly_empty or (parser_succeeded and extraction.get("chars") == 0)
    )
    text_missing = parser_failed or (extraction.get("text_success") is False and not nothing_came_out)
    partial = bool(extraction.get("parse_deadline_reached"))
    # Текст не поместился в потолок: принято начало, остальное отброшено.
    over_the_cap = bool(extraction.get("text_truncated"))
    # Страниц больше, чем разборщик читает. Отдельно от «не уместилось по объёму»:
    # там помогает вопрос о начале документа, а здесь конца тома система не видела
    # вовсе, и знать об этом человеку важнее.
    beyond_the_pages = bool(extraction.get("parse_pages_truncated"))
    read_pages = int(extraction.get("parse_pages_read") or 0)
    total_pages = int(extraction.get("parse_total_pages") or 0)
    pages_line = (
        f" В документе {total_pages} страниц, прочитано {read_pages} — по концу спрашивать бесполезно."
        if beyond_the_pages and total_pages
        else ""
    )
    # Скан без текстового слоя читается ГЛАЗАМИ модели, и в запрос уходит лишь
    # несколько страниц: распознавание стоит места. Цена честная, молчание о ней —
    # нет. Скан на сорок страниц читался по четырём картинкам, а человек получал
    # документ в полной уверенности, что прочитано всё.
    # Поля ПЛОСКИЕ, а не вложенный словарь: публичная проекция
    # (`public_chat_ingestion`) пропускает наружу только перечисленные имена, и
    # вложенный `vision` до моста не доезжает вовсе. Ровно на этом уже обжигались
    # с `parse_pages_truncated`: правка доехала до базы и не доехала до человека.
    vision_total = int(extraction.get("vision_pages_total") or 0)
    vision_read = int(extraction.get("vision_pages_read") or 0)
    vision_line = (
        f" Текста в файле нет — распознавала по картинкам: посмотрено {vision_read} "
        f"страниц из {vision_total}, про остальные я ничего не знаю."
        if vision_total > vision_read > 0
        else ""
    )
    # Архив разобран не весь: часть членов не поместилась в бюджет распаковки или
    # оказалась слишком крупной. TAR об этом говорил, ZIP и RAR молчали.
    archive_files = int(extraction.get("archive_files") or 0)
    archive_read = int(extraction.get("archive_files_read") or 0)
    archive_line = ""
    if extraction.get("archive_truncated"):
        archive_line = (
            f" В архиве {archive_files} файлов, разобрано {archive_read} — про остальные я ничего не знаю."
            if archive_files > archive_read
            else (" Архив разобран не целиком: как минимум один файл внутри прочитан только частично.")
        )
    # Исходник обрезан ДО разбора: разборщик читал не весь файл. Признак писался
    # пятью разборщиками и не читался ни одним потребителем.
    source_clipped = bool(extraction.get("source_truncated_for_parse"))
    # Причина отказа известна коду; человеку доставалось одинаковое «текст извлечь
    # не удалось» и для битого файла, и для незнакомого формата — а следующий шаг
    # у них разный: один пересохранить, другой прислать в другом виде.
    unsupported = bool(extraction.get("unsupported_format"))
    # Reliability is independent from the lifecycle verdict.  In particular,
    # a stale or contradictory backend receipt must not make an unreadable or
    # partial file look whole merely because it says ``promoted`` (and an
    # ``unknown`` action must not hide the warning either).  Build the warning
    # once, then add whichever lifecycle prefix belongs to the receipt.
    warning = ""
    if archive_line:
        warning = archive_line
    elif vision_line:
        warning = vision_line
    elif beyond_the_pages:
        warning = pages_line
    elif unsupported:
        warning = " Такой формат я пока не читаю — пришлите его в PDF, DOCX или текстом."
    elif text_missing:
        warning = " Текст извлечь не удалось: я вижу файл, но не его содержимое."
    elif over_the_cap:
        warning = (
            " Документ длиннее, чем помещается целиком, — принято начало;"
            " по концу файла спрашивать бесполезно."
        )
    elif partial:
        # Успех и полнота — разные вещи: разбор, оборванный по сроку, приходит
        # с `success=True` и частичным текстом. Флаг для этого случая писался
        # в ответ, но не читался ни одним потребителем.
        pages = int(extraction.get("parse_pages_read") or 0)
        read = f" Прочитано страниц: {pages}." if pages else ""
        warning = f" Разбор остановлен по сроку — принято только начало.{read}"
    elif source_clipped:
        warning = " Файл длиннее, чем берёт разбор, — прочитано его начало."
    elif nothing_came_out:
        warning = " Текста в файле не оказалось — разбор прошёл, а содержимого нет."

    if file_ingestion.get("action") == "transient":
        return f"{_FILE_TRANSIENT_STATUS}{warning}"
    if file_ingestion.get("promoted"):
        return f"{_FILE_PROMOTED_STATUS}{warning}"
    if file_ingestion.get("queued_for_review") or file_ingestion.get("inbox_id"):
        return f"{_FILE_REVIEW_STATUS}{warning}"
    return warning.strip()


def _file_chat_warning_line(file_ingestion: Any) -> str:
    """Only a user-impacting file warning, never Inbox/lifecycle bookkeeping.

    The full receipt remains available to the dedicated Inbox/Admin surfaces.
    Ordinary conversation should contain the answer and facts that affect its
    reliability (for example, an unreadable or truncated attachment), but not
    internal state announcements such as "waiting in /inbox" or "became
    knowledge".  Deriving the warning from the same formatter keeps the two
    surfaces consistent without copying the fairly involved extraction logic.
    """

    if not isinstance(file_ingestion, dict):
        return ""
    if file_ingestion.get("archive_password_required") or file_ingestion.get("archive_password_invalid"):
        # The code-owned response already tells the person exactly what is
        # needed.  A generic “text extraction failed” companion makes a normal
        # locked archive sound corrupt and obscures the actionable prompt.
        return ""
    if file_ingestion.get("voice_unrecognised"):
        return _VOICE_UNRECOGNISED_CHAT_WARNING
    fate = _file_fate_line(file_ingestion)
    for status in (_FILE_PROMOTED_STATUS, _FILE_REVIEW_STATUS, _FILE_TRANSIENT_STATUS):
        if fate == status:
            return ""
        if fate.startswith(status):
            return fate[len(status) :].strip()
    return fate


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
            if family == "obs" and action in {"check", "select", "opened", "retry", "cancel"}:
                # Onboarding exposes a private Device ID and mutates a profile.
                # Bind it to the private chat owner before making the already
                # signed backend request; callbacks from groups or forwarded
                # markup cannot operate another person's setup.
                if str(chat.get("type") or "") != "private" or external_user_id != str(chat_id):
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
                if len(data.encode("utf-8")) > 64:
                    raise PermanentUpdateError("Obsidian callback exceeds Telegram limit")
                if action == "select":
                    if target_id == "current":
                        raise PermanentUpdateError("Invalid Obsidian device candidate")
                    path = "/api/obsidian/onboarding/select-device"
                    payload: dict[str, Any] | None = {"candidate_id": target_id}
                else:
                    if target_id != "current":
                        raise PermanentUpdateError("Invalid Obsidian onboarding target")
                    path = {
                        "check": "/api/obsidian/onboarding/check",
                        "opened": "/api/obsidian/onboarding/confirm-open",
                        "retry": "/api/obsidian/onboarding/retry",
                        "cancel": "/api/obsidian/onboarding/cancel",
                    }[action]
                    payload = None
                response = await self._backend_json(
                    backend,
                    "POST",
                    path,
                    payload,
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    {
                        "check": "Статус обновлён",
                        "select": "Устройство выбрано",
                        "opened": "Проверяю подключение",
                        "retry": "Повторяю шаг",
                        "cancel": "Настройка отменена",
                    }[action],
                )
                panel_text, panel_markup = obsidian_panel(response)
                await self._send_message(
                    telegram,
                    chat_id,
                    panel_text,
                    reply_markup=panel_markup,
                )
                clear_markup = True
            elif family == "inbox" and action in {"promote", "ignore"}:
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
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
            elif family == "doc" and action == "more":
                # «{id}.{смещение}» — то же устройство, что у отмены правки
                # сущности: место едет в кнопке, а не хранится в мосте.
                document_id, _, raw_offset = target_id.rpartition(".")
                if not document_id or not raw_offset.isdigit():
                    raise PermanentUpdateError("Invalid document offset")
                document = await self._backend_json(
                    backend,
                    "GET",
                    f"/api/knowledge/{document_id}",
                    None,
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Читаю дальше")
                await self._send_message(
                    telegram,
                    chat_id,
                    self._format_full_document(document, offset=int(raw_offset)),
                    reply_markup=self._document_more_markup(document, document_id, int(raw_offset)),
                )
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
                # Кнопка удаления едет вместе с документом. До этого запись,
                # созданную из чата, нельзя было ни исправить, ни удалить оттуда
                # же: `PATCH`/`DELETE` в `api/knowledge.py` есть с самого начала,
                # а мост звал только `GET`. Человек, сказавший «запомни» и тут же
                # заметивший ошибку, шёл в админку — при том что чат основной
                # интерфейс. Удаление мягкое и обратимое, но подтверждение всё
                # равно спрашивается: одно нажатие мимо не должно уносить запись.
                more = self._document_more_markup(document, target_id, 0)
                rows = list(more["inline_keyboard"]) if more else []
                # Кнопка несёт id того, КОМУ её показали, — как уже делает `conv`.
                # Сообщение видно всему чату, и без привязки любая другая способная
                # учётка, нажав первой, действовала бы на чужом экране. Найдено
                # аудитом Grok по пути ответа (2026-08-07): у `conv`, `ent` и
                # `relation` привязка была, у заведённого мной `know` — нет.
                rows.append(
                    [
                        {"text": "Исправить", "callback_data": f"know:fix:{target_id}.{external_user_id}"},
                        {
                            "text": "Удалить запись",
                            "callback_data": f"know:del:{target_id}.{external_user_id}",
                        },
                    ]
                )
                await self._send_message(
                    telegram,
                    chat_id,
                    self._format_full_document(document),
                    reply_markup={"inline_keyboard": rows},
                )
            elif family == "acc" and action == "grant":
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
                # Право выдаёт backend, а не мост: `admin.users.manage` проверяется
                # там же, где и всегда. Нажатие не владельца просто получит отказ,
                # и он будет назван — `refusal_notice` уже различает «нет права».
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/admin/users/{target_id}/preset",
                    {"preset_key": "user", "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(telegram, callback_id, "Доступ выдан")
                await self._send_message(
                    telegram,
                    chat_id,
                    "Доступ расширен: человеку открыты миссии и остальное, что есть у обычного участника.",
                )
                clear_markup = True
            elif family == "know" and action == "fix":
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
                # Правка использует тот же механизм ответа на реплику, который
                # появился в 0.175.0, и это не совпадение: заводить ради неё
                # второй способ ввода значило бы выбросить его при первой же
                # встрече с первым. Человек отвечает НА приглашение, поэтому
                # адресат однозначен даже в чате, где идёт несколько разговоров.
                prompt_id = await self._send_message_returning_id(
                    telegram,
                    chat_id,
                    "Ответьте на ЭТО сообщение новым текстом записи — я заменю им нынешний. "
                    "Заголовок и связи останутся прежними.",
                )
                if prompt_id:
                    self._inbox.remember_edit_prompt(prompt_id, target_id)
                await self._answer_callback(telegram, callback_id, "Жду новый текст")
            elif family == "know" and action in {"del", "delok"}:
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
                if action == "del":
                    await self._answer_callback(telegram, callback_id, "Точно удалить?")
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Удалить эту запись из знаний? Она станет невидимой для поиска и "
                        "ответов; восстановить можно в админке до истечения срока хранения.",
                        reply_markup={
                            "inline_keyboard": [
                                [
                                    {
                                        "text": "Да, удалить",
                                        "callback_data": f"know:delok:{target_id}.{external_user_id}",
                                    }
                                ]
                            ]
                        },
                    )
                else:
                    await self._backend_json(
                        backend,
                        "DELETE",
                        f"/api/knowledge/{target_id}",
                        None,
                        external_user_id,
                        str(chat_id),
                    )
                    await self._answer_callback(telegram, callback_id, "Запись удалена")
                    await self._send_message(
                        telegram,
                        chat_id,
                        "Запись удалена из знаний. Поиск и ответы её больше не увидят.",
                    )
                    clear_markup = True
            elif family == "ent" and action == "undo":
                # `target_id` — «{id сущности}.{версия}». Версия едет в кнопке, а не
                # вычисляется здесь заново: между показом карточки и нажатием могла
                # вклиниться другая правка, и «отменить последнюю» отменило бы уже
                # не то, что человек видел на экране. Если версии больше нет —
                # backend ответит 404, и это правильный отказ, а не тихий успех.
                # Цель — «{id}.{версия}.{id нажавшего}»: три части, потому что
                # версия и нажавший отвечают на разные вопросы. Версия защищает от
                # чужой правки, вклинившейся между показом и нажатием; привязка —
                # от чужого нажатия по кнопке, показанной не ему.
                head, _, pressed_by = target_id.rpartition(".")
                entity_id, _, raw_version = head.partition(".")
                if not entity_id or not raw_version.isdigit():
                    raise PermanentUpdateError("Invalid entity undo target")
                if pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
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
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
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
            elif family == "relation" and action in {"accept", "reject"}:
                candidate_id, separator, invoker_id = target_id.rpartition(".")
                if not separator or not candidate_id or not invoker_id.isdigit():
                    raise PermanentUpdateError("Invalid relation review target")
                # Buttons in an allowlisted group are visible to everyone. The
                # decision must still belong to the person who requested this
                # review card, not to whichever capable participant taps first.
                if invoker_id != external_user_id:
                    await self._answer_callback(
                        telegram,
                        callback_id,
                        "Эта кнопка не для вас",
                        alert=True,
                    )
                    return
                status = "accepted" if action == "accept" else "rejected"
                await self._backend_json(
                    backend,
                    "POST",
                    f"/api/kg/relation-candidates/{quote(candidate_id, safe='')}/review",
                    {"status": status, "telegram_user": user},
                    external_user_id,
                    str(chat_id),
                )
                await self._answer_callback(
                    telegram,
                    callback_id,
                    "Связь принята" if action == "accept" else "Связь отклонена",
                )
                clear_markup = True
            elif family == "conflict" and action in {"dismiss", "keep_a", "keep_b"}:
                # Цель приходит как «{id}.{id нажавшего}»: идентификаторы записей
                # точек не содержат, поэтому разделение по последней однозначно.
                target_id, _, pressed_by = target_id.rpartition(".")
                if not target_id or pressed_by != external_user_id:
                    await self._answer_callback(telegram, callback_id, "Эта кнопка не для вас", alert=True)
                    return
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
            # «Действие уже недоступно» — правда только для устаревшей кнопки.
            # Отказ ПО ПРАВАМ и «нет такого» — утверждения о разных вещах, и
            # человек, которому не хватает права, принимал общую фразу за поломку
            # и жал ещё раз. `refusal_notice` уже умеет их различать, но сюда его
            # никто не звал: тот же разбор стоял только на текстовых командах.
            notice = refusal_notice(exc)
            await self._answer_callback(
                telegram,
                callback_id,
                notice or "Действие уже недоступно",
                alert=True,
            )
            clear_markup = True
            LOGGER.info("Telegram callback rejected")
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
        except Exception as exc:
            # Косметика: действие уже выполнено, перерисовка не стоит ретрая.
            LOGGER.debug("Could not edit Telegram inline keyboard (%s)", type(exc).__name__)

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
    def _response_reply_markup(response: dict[str, Any], *, external_user_id: str) -> dict[str, Any] | None:
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
            # Кнопка несёт id того, КОМУ её показали. `external_user_id` —
            # обязательный именованный параметр, а не значение по умолчанию:
            # забытая привязка должна ломаться вызовом, а не молча раздавать
            # кнопку всему чату.
            keyboard.append(
                [
                    {
                        "text": "✓ Подтвердить знание",
                        "callback_data": f"inbox:promote:{inbox_id}.{external_user_id}",
                    },
                    {
                        "text": "✕ Игнорировать",
                        "callback_data": f"inbox:ignore:{inbox_id}.{external_user_id}",
                    },
                ]
            )
        return {"inline_keyboard": keyboard}

    @staticmethod
    def _format_response_message(response: dict[str, Any]) -> str:
        raw_message = response.get("message")
        web_sources = _web_source_chat_lines(response.get("web_sources"))
        exact_shape_has_companion = bool(
            any(
                str(response.get(field) or "").strip()
                for field in (
                    "grounding_warning",
                    "regenerate_notice",
                    "verification_caution",
                    "web_query_notice",
                    "citation_notice",
                )
            )
            or response.get("citations")
            or web_sources
            or _file_chat_warning_line(response.get("file_ingestion"))
        )
        if (
            response.get("exact_text_shape_owned") is True
            and isinstance(raw_message, str)
            and raw_message
            and not exact_shape_has_companion
        ):
            # The runtime proved a context-empty, closed surface contract and
            # already rejected every truth/safety companion.  Banners and
            # notices are otherwise useful, but here even one extra line would
            # falsify the requested exact shape.
            return raw_message
        message = str(response.get("message") or "Готово.").strip() or "Готово."
        if web_sources:
            message = _neutralize_unowned_autolinks(message)
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
        file_warning = _file_chat_warning_line(response.get("file_ingestion"))
        if file_warning:
            body = f"{body}\n\n{file_warning}"
        caution = str(response.get("verification_caution") or "").strip()
        if caution:
            body = f"{body}\n\n{caution}"
        if web_sources:
            body = f"{body}\n\nИсточники:\n" + "\n".join(web_sources)
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
        except Exception as exc:
            LOGGER.warning("tts: sendVoice failed (%s)", type(exc).__name__)
            return
        if voice.get("truncated"):
            # Ответ не поместился в клип целиком. Молчать об этом нельзя: рядом
            # лежит полный текст, и человек должен знать, что услышал не всё.
            with suppress(Exception):
                await self._send_message(
                    telegram,
                    chat_id,
                    "Ответ длиннее, чем помещается в голосовое, — озвучено начало. Полный текст выше.",
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

        Доставка возобновляемая: успешный sendDocument сразу получает локальный
        checkpoint, а transient-сбой оставляет update в очереди. Повтор поэтому
        досылает этот файл, не дублируя уже доставленные документы.
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
            except (ValueError, TypeError, binascii.Error):
                LOGGER.warning("make_file: вложение не разобралось")
                continue
            filename = str(item.get("filename") or "report.bin")
            artifact_id = str(item.get("id") or "").strip()
            if not artifact_id:
                artifact_id = hashlib.sha256(
                    payload
                    + b"\0"
                    + filename.encode("utf-8", errors="replace")
                    + b"\0"
                    + str(item.get("mime_type") or "").encode("ascii", errors="replace")
                ).hexdigest()
            delivery_key = hashlib.sha256(
                f"{int(chat_id)}:{artifact_id}".encode("utf-8", errors="strict")
            ).hexdigest()
            if self._inbox.generated_file_was_delivered(delivery_key):
                continue
            try:
                await self._send_document(
                    telegram,
                    chat_id,
                    filename,
                    payload,
                    mime_type=str(item.get("mime_type") or "application/octet-stream"),
                )
            except Exception as exc:
                LOGGER.warning("make_file: sendDocument не удался (%s)", type(exc).__name__)
                # Leave the durable update pending. The text reply has its own
                # chunk checkpoint and successful files are checkpointed below,
                # so retry resumes at this exact document instead of asking the
                # model to rebuild it or duplicating earlier documents.
                raise
            self._inbox.remember_generated_file_delivery(delivery_key)

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
        except Exception as exc:
            LOGGER.info("Could not answer Telegram callback query (%s)", type(exc).__name__)

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
        except Exception as exc:
            LOGGER.debug("Could not clear Telegram inline keyboard (%s)", type(exc).__name__)
