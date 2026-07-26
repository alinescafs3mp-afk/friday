"""Telegram bridge: photos, documents, voice and the rest of what a message can carry.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    _SINGLE_MEDIA_FIELDS,
    Any,
    BridgeShared,
    MediaTooLargeError,
    Path,
    PermanentUpdateError,
    base64,
    httpx,
)


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
        if size and size > self.config.max_document_bytes:
            raise MediaTooLargeError("Telegram media exceeds configured size limit")
        file_id = str(descriptor.get("file_id") or "")
        if not file_id:
            raise PermanentUpdateError("Telegram media has no file_id")

        response = await telegram.post(f"{self._api_url}/getFile", json={"file_id": file_id})
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
                if declared_length > self.config.max_document_bytes:
                    raise MediaTooLargeError("Downloaded media exceeds configured size limit")
            async for chunk in download.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > self.config.max_document_bytes:
                    raise MediaTooLargeError("Downloaded media exceeds configured size limit")
                chunks.append(chunk)
        content = b"".join(chunks)
        prepared: dict[str, Any] = {
            "filename": Path(filename).name,
            "mime_type": mime_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "source_ref": f"telegram-file:{update['update_id']}:{file_id}",
            "media_kind": media_kind,
        }
        duration = descriptor.get("duration")
        if isinstance(duration, int) and duration > 0:
            prepared["duration"] = duration
        return prepared

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
