"""Telegram bridge: photos, documents, voice and the rest of what a message can carry.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import re

from friday.telegram_bridge._base import (
    _SINGLE_MEDIA_FIELDS,
    BOT_API_DOWNLOAD_LIMIT_BYTES,
    Any,
    BridgeShared,
    MediaTooLargeError,
    Path,
    PermanentUpdateError,
    base64,
    httpx,
)

#: Сколько альбомов помнить одновременно. Группа живёт секунды, десяти хватает с
#: запасом на несколько человек, пишущих разом; без потолка словарь рос бы всю
#: жизнь процесса.
_ALBUM_CAPTION_MEMORY = 10

# `MediaTooLargeError` carries the text the user reads in the chat, so the reason
# must be the true one: «настроенный предел» and «потолок Telegram» ask for
# different next steps from the sender.
_BOT_API_LIMIT_MESSAGE = (
    "Telegram не отдаёт ботам файлы больше 20 МБ — файл не сохранён. "
    "Сожмите его или разбейте на части и пришлите снова."
)
_CONFIGURED_LIMIT_MESSAGE = (
    "Файл слишком большой — Telegram-медиа превышает допустимый размер и не сохранено."
)
_REPLY_DOCUMENT_SOURCE_REF_MAX_CHARS = 500


class MediaMixin(BridgeShared):
    _UNSUPPORTED_LABELS = (
        ("sticker", "стикер"),
        ("poll", "опрос"),
        ("dice", "кубик"),
        ("game", "игру"),
        ("story", "историю"),
    )

    @staticmethod
    def _select_media(
        message: dict[str, Any], update: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, str, str]:
        """Pick the media descriptor to download and its (filename, mime, media_kind)."""
        message_id = message.get("message_id", update["update_id"])
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            descriptor = max(
                (item for item in photos if isinstance(item, dict)),
                key=lambda item: int(item.get("file_size", 0)),
                default=None,
            )
            if descriptor is not None:
                return descriptor, f"telegram-photo-{message_id}.jpg", "image/jpeg", "photo"
        for media_field, media_kind, default_mime, suffix, use_file_name in _SINGLE_MEDIA_FIELDS:
            descriptor = message.get(media_field)
            if not isinstance(descriptor, dict):
                continue
            mime_type = str(descriptor.get("mime_type") or default_mime)
            if use_file_name and descriptor.get("file_name"):
                filename = str(descriptor["file_name"])
            else:
                filename = f"telegram-{media_kind.replace('_', '-')}-{message_id}.{suffix}"
            return descriptor, filename, mime_type, media_kind
        return None, "", "application/octet-stream", ""

    @staticmethod
    def _reply_document_source_ref(message: dict[str, Any]) -> str:
        """Return an exact bounded pointer to supported media being replied to.

        This is a structural Telegram reference, not another upload: no bytes are
        downloaded here.  The backend must still re-authorize tenant, uploader and
        the current file verdict before turning it into an opaque Raw id.
        """

        replied = message.get("reply_to_message")
        if not isinstance(replied, dict):
            return ""
        descriptor, _filename, _mime_type, _media_kind = MediaMixin._select_media(
            replied,
            {"update_id": int(replied.get("message_id") or 0)},
        )
        if not isinstance(descriptor, dict):
            return ""
        file_id = str(descriptor.get("file_id") or "").strip()
        if not file_id or any(char.isspace() or ord(char) < 33 for char in file_id):
            return ""
        source_ref = f"telegram-file:{file_id}"
        return source_ref if len(source_ref) <= _REPLY_DOCUMENT_SOURCE_REF_MAX_CHARS else ""

    async def _prepare_document(
        self,
        telegram: httpx.AsyncClient,
        message: dict[str, Any],
        update: dict[str, Any],
    ) -> dict[str, Any] | None:
        descriptor, filename, mime_type, media_kind = self._select_media(message, update)
        if not descriptor:
            return None
        size = int(descriptor.get("file_size") or 0)
        limit = min(self.config.max_document_bytes, BOT_API_DOWNLOAD_LIMIT_BYTES)
        limit_message = (
            _BOT_API_LIMIT_MESSAGE
            if self.config.max_document_bytes >= BOT_API_DOWNLOAD_LIMIT_BYTES
            else _CONFIGURED_LIMIT_MESSAGE
        )
        if size and size > limit:
            raise MediaTooLargeError(limit_message)
        file_id = str(descriptor.get("file_id") or "")
        if not file_id:
            raise PermanentUpdateError("Telegram media has no file_id")

        response = await telegram.post(f"{self._api_url}/getFile", json={"file_id": file_id})
        # Some descriptors carry no file_size, so the first time the ceiling can
        # show up is `getFile` answering 400 «file is too big». That is permanent
        # and the sender must hear the reason, not receive a dead-letter notice.
        if response.status_code == 400:
            try:
                description = str(response.json().get("description") or "")
            except ValueError:
                description = ""
            if "too big" in description.casefold():
                raise MediaTooLargeError(_BOT_API_LIMIT_MESSAGE)
        response.raise_for_status()
        payload = response.json()
        file_path = str((payload.get("result") or {}).get("file_path") or "")
        if not payload.get("ok") or not file_path:
            raise RuntimeError("Telegram getFile did not return a path")
        chunks: list[bytes] = []
        downloaded = 0
        async with telegram.stream("GET", f"{self._file_url}/{file_path}") as download:
            download.raise_for_status()
            content_length = download.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = 0
                if declared_length > limit:
                    raise MediaTooLargeError(limit_message)
            async for chunk in download.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > limit:
                    raise MediaTooLargeError(limit_message)
                chunks.append(chunk)
        content = b"".join(chunks)
        prepared: dict[str, Any] = {
            "filename": Path(filename).name,
            "mime_type": mime_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            # Без `update_id`: он уникален у КАЖДОЙ отправки, поэтому один и тот
            # же файл никогда не совпадал сам с собой, и повторная пересылка
            # заводила второй Raw Object, второй Inbox и второй Knowledge Object.
            # `file_id` у Telegram стабилен для одного файла и одного бота — это
            # и есть ключ происхождения. Содержимое проверяется вторым ключом.
            "source_ref": f"telegram-file:{file_id}",
            "media_kind": media_kind,
        }
        duration = descriptor.get("duration")
        if isinstance(duration, int) and duration > 0:
            prepared["duration"] = duration
        return prepared

    def _group_address(
        self,
        message: dict[str, Any],
        chat: dict[str, Any],
        text: str,
    ) -> tuple[bool, str]:
        """Обращаются ли к Пятнице в этом сообщении — и текст без самого обращения.

        В личной переписке обращением является само сообщение: там `True` всегда.
        В группе — только три вещи: упоминание по `@имени`, ответ на сообщение
        самой Пятницы и команда. Всё остальное — разговор людей между собой, и
        вмешиваться в него нельзя ни ответом, ни записью в архив.

        Обращение УБИРАЕТСЯ из текста: «@friday_bot что по смете» — это вопрос
        «что по смете», а не вопрос про бота. Оставленное упоминание попадало бы и
        в классификатор, и в поиск, и в сохранённую запись.

        Без известного имени (`getMe` не ответил) упоминание не распознаётся —
        остаются команды и ответы. Это ограничение названо, а не спрятано: тихо
        отвечать всем подряд хуже, чем честно отвечать реже.
        """
        # Требование обращения включается ТОЛЬКО там, где Telegram прямо сказал
        # «группа». Обратное правило («молчать везде, кроме явно личного») было бы
        # закрытым умолчанием — но цена ошибки здесь несимметрична в другую
        # сторону: лишний ответ это шум, а лишнее молчание это сломанный главный
        # интерфейс продукта. Обновление без вида чата ведёт себя как прежде.
        if str(chat.get("type") or "") not in {"group", "supergroup"}:
            return True, text
        if text.startswith("/"):
            return True, text
        reply = message.get("reply_to_message")
        if isinstance(reply, dict):
            author = reply.get("from")
            if isinstance(author, dict) and bool(author.get("is_bot")):
                return True, text
        username = str(getattr(self, "_bot_username", "") or "")
        if not username:
            return False, text
        mention = f"@{username}"
        if mention.casefold() not in text.casefold():
            return False, text
        # Вырезается ровно упоминание, в любом регистре и в любом месте строки:
        # обратиться могут и в начале, и в конце («что по смете, @friday_bot»).
        cleaned = re.sub(re.escape(mention), " ", text, flags=re.IGNORECASE)
        return True, " ".join(cleaned.split())

    def _album_caption(self, message: dict[str, Any]) -> str:
        """Подпись альбома, отданная ВСЕМ его частям.

        Telegram шлёт альбом несколькими сообщениями с общим `media_group_id`, и
        подпись стоит ровно у одной части — обычно у первой. Остальные приходят
        совсем пустыми, поэтому «вот договор, пять страниц» относилось к одному
        файлу из пяти, а четыре попадали в архив без единого слова о том, что это.

        Части одного чата обрабатываются строго по очереди (`ordering_key`),
        поэтому достаточно помнить последнюю группу: подпись доезжает до частей,
        пришедших ПОСЛЕ подписанной. Память живёт в процессе — после рестарта
        поведение честно возвращается к прежнему, а не притворяется.
        """
        group_id = str(message.get("media_group_id") or "")
        if not group_id:
            return ""
        own = str(message.get("caption") or "").strip()
        if own:
            # Потолок словаря: группа живёт секунды, а без ограничения он рос бы
            # всю жизнь процесса. Вытесняется самая старая запись.
            if len(self._album_captions) >= _ALBUM_CAPTION_MEMORY:
                self._album_captions.pop(next(iter(self._album_captions)), None)
            self._album_captions[group_id] = own[:1000]
            return ""
        return self._album_captions.get(group_id, "")

    @staticmethod
    def _reply_quote(message: dict[str, Any]) -> str:
        """Текст сообщения, НА КОТОРОЕ человек ответил репликой.

        Пустая строка означает «ответа ни на что не было» — обычное сообщение.
        Берётся и `text`, и `caption`: ответить можно и на картинку с подписью.
        Длина ограничена здесь же, у источника: отвечают и на документ в тысячу
        строк, а смысл только в том, НА ЧТО человек показал.
        """
        replied = message.get("reply_to_message")
        if not isinstance(replied, dict):
            return ""
        text = str(replied.get("text") or replied.get("caption") or "").strip()
        return text[:1000]

    @staticmethod
    def _extract_forward(message: dict[str, Any]) -> dict[str, Any]:
        """Capture forwarded-message provenance for the Raw Object, if present."""
        forward: dict[str, Any] = {}
        origin = message.get("forward_origin")
        if isinstance(origin, dict):
            forward["origin"] = origin
        from_user = message.get("forward_from")
        if isinstance(from_user, dict):
            forward["from_user"] = {
                key: from_user.get(key)
                for key in ("id", "username", "first_name", "last_name")
                if from_user.get(key) is not None
            }
        from_chat = message.get("forward_from_chat")
        if isinstance(from_chat, dict):
            forward["from_chat"] = {
                key: from_chat.get(key)
                for key in ("id", "title", "username", "type")
                if from_chat.get(key) is not None
            }
        sender_name = message.get("forward_sender_name")
        if sender_name:
            forward["sender_name"] = str(sender_name)
        forward_date = message.get("forward_date")
        if isinstance(forward_date, int):
            forward["date"] = forward_date
        from_message_id = message.get("forward_from_message_id")
        if isinstance(from_message_id, int):
            forward["from_message_id"] = from_message_id
        return forward

    @staticmethod
    def _structured_text(message: dict[str, Any]) -> str | None:
        """Turn a location/venue/contact message into a plain-text note to ingest."""
        location = message.get("location")
        if isinstance(location, dict) and location.get("latitude") is not None:
            return f"📍 Геолокация: {location.get('latitude')}, {location.get('longitude')}"
        venue = message.get("venue")
        if isinstance(venue, dict):
            parts = [str(venue.get(key) or "") for key in ("title", "address") if venue.get(key)]
            loc = venue.get("location")
            coords = ""
            if isinstance(loc, dict) and loc.get("latitude") is not None:
                coords = f" ({loc.get('latitude')}, {loc.get('longitude')})"
            return f"📍 Место: {', '.join(parts)}{coords}".strip()
        contact = message.get("contact")
        if isinstance(contact, dict):
            name = " ".join(str(contact.get(key) or "") for key in ("first_name", "last_name")).strip()
            phone = str(contact.get("phone_number") or "")
            return f"👤 Контакт: {name or 'без имени'}, {phone}".rstrip(", ")
        return None

    @classmethod
    def _unsupported_label(cls, message: dict[str, Any]) -> str | None:
        for content_field, label in cls._UNSUPPORTED_LABELS:
            if message.get(content_field) is not None:
                return label
        return None
