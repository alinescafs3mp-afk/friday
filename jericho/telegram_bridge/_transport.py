"""Telegram bridge: the long-polling loops, the outbound queue and signed backend calls.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from jericho.telegram_bridge._base import (
    API_BASE,
    BACKOFF_MAX,
    BOT_COMMANDS,
    LOGGER,
    POLL_TIMEOUT,
    TELEGRAM_TEXT_LIMIT,
    Any,
    BridgeShared,
    MediaTooLargeError,
    Path,
    PermanentUpdateError,
    ProcessLease,
    RuntimeLeaseError,
    TelegramConfig,
    _proxy_password,
    _redact_userinfo,
    asyncio,
    httpx,
    install_secret_redaction,
    json,
    sign_bridge_request,
    time,
    uuid,
)
from jericho.telegram_bridge._queue import _UpdateInbox


class TransportMixin(BridgeShared):
    def __init__(self, config: TelegramConfig) -> None:
        config.validate()
        self.config = config
        self._running = False
        self._inbox = _UpdateInbox(config.inbox_db_path)
        inbox_path = Path(config.inbox_db_path)
        self._lease = ProcessLease(
            inbox_path.with_name(f"{inbox_path.name}.lock"),
            protocol="jericho.telegram-bridge.v1",
        )
        self._offset = self._inbox.get_offset()
        self._api_url = f"{API_BASE}/bot{config.bot_token}"
        self._file_url = f"{API_BASE}/file/bot{config.bot_token}"
        self._backend_url = config.backend_url.rstrip("/")

    async def run(self) -> None:
        install_secret_redaction(
            tuple(
                secret
                for secret in (
                    self.config.bot_token,
                    self.config.bridge_secret,
                    _proxy_password(self.config.telegram_proxy),
                )
                if secret
            )
        )
        try:
            self._lease.acquire()
        except RuntimeLeaseError:
            self._inbox.close()
            raise
        self._running = True
        timeout = httpx.Timeout(POLL_TIMEOUT + 10.0, connect=15.0)
        try:
            async with (
                # Only Telegram goes through the proxy. `trust_env` stays off on both
                # clients: the proxy is a deliberate setting, not something a stray
                # HTTPS_PROXY in the environment gets to impose — and the backend is
                # loopback, which such a variable would happily misroute.
                httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    proxy=self.config.telegram_proxy or None,
                ) as telegram,
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self.config.backend_timeout_sec, connect=15.0),
                    trust_env=False,
                ) as backend,
            ):
                LOGGER.info(
                    "Telegram bridge started at offset %d%s",
                    self._offset,
                    f" via proxy {_redact_userinfo(self.config.telegram_proxy)}"
                    if self.config.telegram_proxy
                    else "",
                )
                await self._register_commands(telegram)
                # Inbound polling and outbound push run concurrently; a crash in
                # one loop must not take down the other, so each supervises itself.
                await asyncio.gather(
                    self._poll_loop(telegram, backend),
                    self._outbound_loop(telegram, backend),
                )
        finally:
            self._inbox.close()
            self._lease.release()
            LOGGER.info("Telegram bridge stopped")

    async def _register_commands(self, telegram: httpx.AsyncClient) -> None:
        """Register the command menu once so Telegram shows '/' autocomplete.

        The command surface is otherwise discoverable only by remembering /help.
        Best-effort: a failure here must never stop the bridge from starting.
        """
        payload = {"commands": [{"command": name, "description": desc} for name, desc in BOT_COMMANDS]}
        try:
            response = await telegram.post(f"{self._api_url}/setMyCommands", json=payload)
            response.raise_for_status()
        except Exception:
            LOGGER.warning("Telegram setMyCommands failed (non-fatal)", exc_info=True)

    async def _poll_loop(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._drain_inbox(telegram, backend)
                updates = await self._get_updates(telegram)
                for update in updates:
                    self._inbox.store(update)
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    self._inbox.set_offset(self._offset)
                if updates:
                    await self._drain_inbox(telegram, backend)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Telegram bridge poll loop failed")
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX, backoff * 2)

    async def _outbound_loop(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        """Drain the backend outbound queue and deliver each message to Telegram."""
        while self._running:
            try:
                await self._drain_outbound(telegram, backend)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Telegram bridge outbound loop failed")
            await asyncio.sleep(max(2.0, float(self.config.outbound_poll_interval_sec)))

    async def _drain_outbound(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        signer_chat = str(self.config.allowed_chat_ids[0]) if self.config.allowed_chat_ids else ""
        if not signer_chat:
            return
        data = await self._backend_json(
            backend,
            "GET",
            "/api/notifications/pending?limit=20",
            None,
            signer_chat,
            signer_chat,
        )
        raw_items = data.get("items")
        items: list[Any] = raw_items if isinstance(raw_items, list) else []
        sent: list[str] = []
        failed: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            notif_id = str(item.get("id") or "")
            chat_raw = str(item.get("chat_id") or "")
            body = str(item.get("body") or "")
            if not notif_id or not body:
                continue
            # Deny-by-default re-check at the send edge: the bot token can reach
            # any chat, so an outbound message must target an allowed chat only.
            try:
                if int(chat_raw) not in self.config.allowed_chat_ids:
                    failed.append(notif_id)
                    continue
                chat_id = int(chat_raw)
            except ValueError:
                failed.append(notif_id)
                continue
            try:
                await self._send_message(telegram, chat_id, body)
                sent.append(notif_id)
            except Exception:
                LOGGER.warning("Outbound notification delivery failed", exc_info=True)
                failed.append(notif_id)
            # Gentle per-chat pacing to stay within Telegram send limits.
            await asyncio.sleep(0.05)
        if sent or failed:
            await self._backend_json(
                backend,
                "POST",
                "/api/notifications/ack",
                {"sent": sent, "failed": failed},
                signer_chat,
                signer_chat,
            )

    async def stop(self) -> None:
        self._running = False

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.post(
            f"{self._api_url}/getUpdates",
            json={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload.get('description', 'unknown error')}")
        return [item for item in payload.get("result", []) if isinstance(item, dict)]

    async def _drain_inbox(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
    ) -> None:
        for row in self._inbox.pending():
            update_id = int(row["update_id"])
            update: dict[str, Any] = {}
            try:
                update = json.loads(row["payload_json"])
                cached = (
                    json.loads(row["backend_response_json"]) if row.get("backend_response_json") else None
                )
                await self._process_update(telegram, backend, update, cached_response=cached)
                self._inbox.remove(update_id)
            except PermanentUpdateError as exc:
                LOGGER.warning("Quarantining invalid Telegram update %s: %s", update_id, exc)
                self._inbox.mark_dead_letter(update_id, f"{type(exc).__name__}: {exc}")
                # MediaTooLargeError already told the user; others left them in
                # silence — a rejected message must never just vanish.
                if not isinstance(exc, MediaTooLargeError):
                    await self._notify_dead_letter(telegram, update, permanent=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Telegram update %s deferred: %s", update_id, exc)
                dead_lettered = self._inbox.mark_failure(
                    update_id,
                    f"{type(exc).__name__}: {exc}",
                )
                if dead_lettered:
                    LOGGER.error("Telegram update %s exhausted its retry budget", update_id)
                    await self._notify_dead_letter(telegram, update, permanent=False)
                break

    @staticmethod
    def _update_chat_id(update: dict[str, Any]) -> int | None:
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict):
            return None
        try:
            return int(chat.get("id", 0)) or None
        except (TypeError, ValueError):
            return None

    async def _notify_dead_letter(
        self, telegram: httpx.AsyncClient, update: dict[str, Any], *, permanent: bool
    ) -> None:
        """Tell the originating (allowlisted) chat its message could not be
        processed, so a dead-lettered update is never pure silence. Deny-by-
        default: only allowlisted chats are messaged; best-effort delivery."""
        chat_id = self._update_chat_id(update)
        if chat_id is None or chat_id not in self.config.allowed_chat_ids:
            return
        text = (
            "⚠️ Не удалось обработать это сообщение — оно отклонено."
            if permanent
            else "⚠️ Не удалось обработать это сообщение, я отложил его. Попробуйте позже или переформулируйте."
        )
        try:
            await self._send_message(telegram, chat_id, text)
        except Exception:
            LOGGER.warning("dead-letter notice to chat %s failed", chat_id, exc_info=True)

    async def _backend_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        external_user_id: str,
        chat_id: str,
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        )
        timestamp = int(time.time())
        nonce = uuid.uuid4().hex
        signature = sign_bridge_request(
            self.config.bridge_secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id=external_user_id,
            chat_id=chat_id,
            nonce=nonce,
            body=body,
        )
        headers = {
            "Content-Type": "application/json",
            "X-Jericho-Timestamp": str(timestamp),
            "X-Jericho-User": external_user_id,
            "X-Jericho-Chat": chat_id,
            "X-Jericho-Nonce": nonce,
            "X-Jericho-Signature": signature,
        }
        response = await client.request(
            method,
            f"{self._backend_url}{path}",
            content=body if body else None,
            headers=headers,
        )
        if response.status_code == 409 and not response.headers.get("Retry-After", "").strip():
            detail = response.text[:500]
            raise PermanentUpdateError(f"Backend rejected update (409): {detail}")
        if response.status_code in {400, 403, 404, 413, 422}:
            detail = response.text[:500]
            raise PermanentUpdateError(f"Backend rejected update ({response.status_code}): {detail}")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Backend returned a non-object response")
        return data

    async def _typing_loop(self, client: httpx.AsyncClient, chat_id: int) -> None:
        try:
            while True:
                await client.post(
                    f"{self._api_url}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"},
                )
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("Telegram typing indicator failed", exc_info=True)

    async def _send_message(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        clean = str(text or "").strip() or "Готово."
        chunks: list[str] = []
        while clean:
            if len(clean) <= TELEGRAM_TEXT_LIMIT:
                chunks.append(clean)
                break
            split_at = clean.rfind("\n", 0, TELEGRAM_TEXT_LIMIT)
            if split_at < TELEGRAM_TEXT_LIMIT // 2:
                split_at = clean.rfind(" ", 0, TELEGRAM_TEXT_LIMIT)
            if split_at < TELEGRAM_TEXT_LIMIT // 2:
                split_at = TELEGRAM_TEXT_LIMIT
            chunks.append(clean[:split_at].rstrip())
            clean = clean[split_at:].lstrip()
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            response = await client.post(f"{self._api_url}/sendMessage", json=payload)
            response.raise_for_status()
