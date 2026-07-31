"""Telegram bridge: the long-polling loops, the outbound queue and signed backend calls.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

from contextlib import suppress

from jericho.telegram_bridge._base import (
    API_BASE,
    BACKOFF_MAX,
    BATCH_SIZE,
    BOT_COMMANDS,
    LOGGER,
    POLL_TIMEOUT,
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
    split_for_telegram,
    time,
    uuid,
)
from jericho.telegram_bridge._queue import _UpdateInbox
from jericho.telemetry.logging import redact_text


def _bridge_redactor(config: TelegramConfig):
    """Remove this bridge's own credentials from a string, exactly and by shape.

    Exact values first — the bot token is not a recognisable shape, it is a
    specific string, and the URL it lives in (`.../bot<token>/sendMessage`) is
    what httpx quotes back inside every error. Then the generic redaction, for
    everything a message might carry that this bridge does not know about.
    """
    secrets = tuple(
        value
        for value in (config.bot_token, config.bridge_secret, _proxy_password(config.telegram_proxy))
        if value and len(value) >= 8
    )

    def redact(value: object) -> str:
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        return redact_text(text)

    return redact


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
        # The API URL CONTAINS the bot token, and httpx puts the URL in the text
        # of every HTTPStatusError. Those strings were stored verbatim on the
        # queue row and then surfaced by `jericho doctor`, `jericho status
        # --json` and `GET /api/admin/diagnostics` — the credential printed by
        # the health check. `install_secret_redaction` covers the LOG handlers;
        # this covers what is written into the database.
        self._redact = _bridge_redactor(config)
        self._backend_url = config.backend_url.rstrip("/")
        # Last known failing/healthy state per loop, for transition detection.
        self._loop_failing: dict[str, bool] = {}
        self._warned_no_signer = False
        # Сторож backend: мост — первый, кто узнаёт о его смерти, и единственный,
        # кто может об этом сказать (sentinel живёт ВНУТРИ backend и молчит с ним).
        self._backend_down_since = 0.0
        self._backend_down_warned_at = 0.0

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

    def _signer_chat_id(self) -> str:
        """The chat the bridge signs its OWN service calls as. Must be a person.

        It used to be ``allowed_chat_ids[0]``, and the effective allowlist is
        ``sorted()`` — so a single group in it lands a NEGATIVE id in position zero.
        The backend's ``verify_bridge_request`` requires ``external_user_id.isdigit()``
        and a leading minus fails that, so every signed service call was rejected:
        the outbound queue **never drained** and no proactive notification ever
        arrived, silently, for as long as the group stayed allowlisted.

        Since 0.75.0 that rejection is also a 401 that spends the per-IP
        auth-failure budget, once every poll interval — so the bridge eventually
        rate-limited itself, and the owner shares the loopback address with it.

        A group id is a room, not a person; only a positive id identifies a user.
        """
        for chat_id in self.config.allowed_chat_ids:
            if chat_id > 0:
                return str(chat_id)
        return ""

    def _log_loop_failure(self, loop_name: str, error: BaseException) -> None:
        """Трейсбек — ОДИН на эпизод недоступности, а не на каждую попытку.

        Замерено на живом журнале: 6.43 МБ файла, из них 6.39 МБ (99.5%) — сцепленные
        трейсбеки httpx по ~5 КБ каждый, 1368 штук у опроса и 70 у отправки. Причина
        одна и та же на всех: `LOGGER.exception` стоял внутри цикла, у которого уже
        есть экспоненциальный откат, — то есть печатался на каждой попытке
        переподключения.

        Под этим шумом похоронено то, что действительно стоит знать. Переходы
        «сломалось/починилось» пишутся отдельно и в `runtime_events`: 300 падений
        опроса против 299 восстановлений, суммарно 6 ч 13 мин недоступности за четверо
        суток (6.42%), самый длинный обрыв — 49 минут 27 секунд. Ротация журналов
        работает, но крутила почти исключительно этот шум.

        Первая неудача эпизода печатается со стеком: он нужен, чтобы понять, ЧТО
        сломалось. Дальнейшие — одной строкой с именем исключения: они говорят только
        «всё ещё не работает», и это уже сказано.
        """
        if self._loop_failing.get(loop_name) is not True:
            LOGGER.exception("Telegram bridge %s loop failed", loop_name)
        else:
            LOGGER.warning("Telegram bridge %s loop still failing: %s", loop_name, type(error).__name__)

    async def _journal_transition(
        self,
        backend: httpx.AsyncClient,
        loop_name: str,
        *,
        failing: bool,
        error: BaseException | None = None,
    ) -> None:
        """Report a loop starting to fail, and starting to work again.

        Transitions rather than ticks, for the reason the workers use the same rule: a
        tunnel outage on this machine produced 295 consecutive polling failures across
        three days. As ticks that is 295 rows saying one thing; as transitions it is two.

        The bridge cannot write ``runtime_events`` itself — separate process, separate
        database — so this posts to the backend, which is on loopback and therefore
        still reachable when the tunnel that broke Telegram is down. Best effort in the
        strongest sense: journalling must never be why the loop it observes stops.
        """
        previous = self._loop_failing.get(loop_name)
        if previous == failing:
            return
        self._loop_failing[loop_name] = failing
        if previous is None and not failing:
            return  # the first successful round after start is not a recovery
        signer = self._signer_chat_id()
        if not signer:
            return
        payload: dict[str, Any] = {"loop": loop_name}
        if error is not None:
            # The type, never the message: an exception from an HTTP client can carry a
            # full URL, and the bot token lives in Telegram URLs.
            payload["error_type"] = type(error).__name__
        try:
            await self._backend_json(
                backend,
                "POST",
                "/api/events",
                {
                    "event_type": f"bridge.{loop_name}_{'failed' if failing else 'recovered'}",
                    "payload": payload,
                },
                signer,
                signer,
            )
        except Exception:
            LOGGER.debug("Could not journal bridge %s transition", loop_name, exc_info=True)

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
                await self._journal_transition(backend, "poll", failing=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_loop_failure("poll", exc)
                await self._journal_transition(backend, "poll", failing=True, error=exc)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX, backoff * 2)

    async def _outbound_loop(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        """Drain the backend outbound queue and deliver each message to Telegram."""
        while self._running:
            try:
                await self._drain_outbound(telegram, backend)
                await self._journal_transition(backend, "outbound", failing=False)
                await self._notify_backend_recovered(telegram)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_loop_failure("outbound", exc)
                await self._journal_transition(backend, "outbound", failing=True, error=exc)
                await self._warn_owner_if_backend_down(telegram, backend)
            await asyncio.sleep(max(2.0, float(self.config.outbound_poll_interval_sec)))

    # Сколько backend должен молчать, прежде чем мост скажет об этом владельцу.
    # Меньше — и каждый рестарт супервизора (2 с) превращается в тревогу.
    _BACKEND_DOWN_GRACE_SEC = 120.0
    # Повтор тревоги по той же аварии — не чаще раза в сутки, как у sentinel.
    _BACKEND_DOWN_REPEAT_SEC = 86_400.0

    async def _warn_owner_if_backend_down(
        self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient
    ) -> None:
        """Прямое sendMessage владельцу, минуя очередь уведомлений.

        Вся самодиагностика (sentinel) исполняется внутри backend и доставляется
        через очередь, которую разгребает этот же мост С backend-а — то есть о
        смерти backend не могло сообщить НИЧТО: владелец узнавал по тишине бота
        спустя часы. Мост опрашивает backend каждые 15 секунд, держит токен бота
        и список чатов — он первый узнаёт и единственный может сказать.
        """
        try:
            response = await backend.get(f"{self._backend_url}/health")
            if int(response.status_code) < 500:
                self._backend_down_since = 0.0
                return
        except Exception:  # noqa: BLE001 - недоступность и есть проверяемое состояние
            pass
        now = time.monotonic()
        if not self._backend_down_since:
            self._backend_down_since = now
            return
        if now - self._backend_down_since < self._BACKEND_DOWN_GRACE_SEC:
            return
        if (
            self._backend_down_warned_at
            and now - self._backend_down_warned_at < self._BACKEND_DOWN_REPEAT_SEC
        ):
            return
        signer_chat = self._signer_chat_id()
        if not signer_chat:
            return
        minutes = int((now - self._backend_down_since) / 60)
        try:
            answer = await telegram.post(
                f"{self._api_url}/sendMessage",
                json={
                    "chat_id": int(signer_chat),
                    "text": (
                        f"⚠️ Backend Jericho не отвечает уже ~{minutes} мин. "
                        "Бот принимает сообщения, но обработка стоит; они дойдут после "
                        "восстановления. Проверьте процесс jericho up на хосте."
                    ),
                },
            )
            answer.raise_for_status()
            self._backend_down_warned_at = now
        except Exception:  # noqa: BLE001 - тревога не должна ронять цикл
            LOGGER.warning("Could not warn the owner about the backend outage", exc_info=True)

    async def _notify_backend_recovered(self, telegram: httpx.AsyncClient) -> None:
        """Одно сообщение о восстановлении — тревога без отбоя учит игнорировать тревоги."""
        self._backend_down_since = 0.0
        if not self._backend_down_warned_at:
            return
        self._backend_down_warned_at = 0.0
        signer_chat = self._signer_chat_id()
        if not signer_chat:
            return
        with suppress(Exception):
            await telegram.post(
                f"{self._api_url}/sendMessage",
                json={
                    "chat_id": int(signer_chat),
                    "text": "✅ Backend Jericho снова отвечает; накопившиеся сообщения обрабатываются.",
                },
            )

    async def _drain_outbound(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        signer_chat = self._signer_chat_id()
        if not signer_chat:
            # Said once, loudly. Returning in silence every fifteen seconds is how
            # a broken outbound channel looks exactly like a quiet one.
            if not self._warned_no_signer:
                self._warned_no_signer = True
                LOGGER.error(
                    "No private chat in the allowlist: the bridge cannot sign its own "
                    "service calls, so proactive notifications will never be delivered. "
                    "Add the owner's private chat id to JERICHO_TELEGRAM_ALLOWED_CHAT_IDS "
                    "or JERICHO_TELEGRAM_OWNER_CHAT_IDS."
                )
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
            await self._ack_outbound(backend, signer_chat, sent, failed)

    async def _ack_outbound(
        self,
        backend: httpx.AsyncClient,
        signer_chat: str,
        sent: list[str],
        failed: list[str],
    ) -> None:
        """Report the batch's outcome, retrying in place before giving up.

        Delivery state lives ONLY in this local list until the ack lands, so a
        single failed ack means every message of the batch — up to twenty — is
        still `pending` and gets delivered to the user AGAIN on the next cycle,
        fifteen seconds later, and on every cycle until an ack succeeds. The
        window is the whole batch: twenty sends, each a round trip to Telegram.

        Acking each message right after its send would shrink the window to one,
        and is NOT done: the bridge signs its service calls as the owner, so they
        count against `telegram:user:<owner>` — 30 requests per minute by default,
        shared with the owner's own messages. Twenty acks per drain would spend
        that budget on bookkeeping and start 429-ing real traffic.

        So the ack is retried here instead: three attempts inside the drain, which
        turns "one transient 5xx or one backend restart" into "three failures in a
        row within two seconds". The complete fix is an `in_flight` lease on the
        row itself, which is a schema change and needs its own expiry story —
        deliberately not started at the tail of this work.
        """
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await self._backend_json(
                    backend,
                    "POST",
                    "/api/notifications/ack",
                    {"sent": sent, "failed": failed},
                    signer_chat,
                    signer_chat,
                )
                return
            except Exception as exc:  # noqa: PERF203 - the retry IS the point here
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        # Loud, because the consequence is user-visible: these messages were
        # delivered and will be delivered again.
        LOGGER.error(
            "Outbound ack failed after retries: %d delivered notifications will be re-sent",
            len(sent),
            exc_info=last_error,
        )

    async def stop(self) -> None:
        self._running = False

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.post(
            f"{self._api_url}/getUpdates",
            json={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                # edited_message принимается, чтобы честно ответить «правки не
                # подхватываю» — иначе чат и база молча расходятся навсегда.
                "allowed_updates": ["message", "edited_message", "callback_query"],
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
        # The budget counts attempted rows, not SELECT rounds. `pending()` exposes
        # only the ready head of each chat; after a success/removal the next update
        # of that same chat can become eligible in this drain. A retryable failure
        # blocks only its own chat for the rest of this call, while other chats keep
        # spending the bounded batch. This preserves per-chat FIFO across retry
        # delays without restoring global head-of-line blocking.
        attempted = 0
        blocked_keys: set[str] = set()
        while attempted < BATCH_SIZE:
            remaining = BATCH_SIZE - attempted
            rows = [
                row
                for row in self._inbox.pending(limit=remaining + len(blocked_keys))
                if str(row["ordering_key"]) not in blocked_keys
            ][:remaining]
            if not rows:
                break
            for row in rows:
                attempted += 1
                update_id = int(row["update_id"])
                ordering_key = str(row["ordering_key"])
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
                    self._inbox.mark_dead_letter(
                        update_id,
                        self._redact(f"{type(exc).__name__}: {exc}"),
                    )
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
                        self._redact(f"{type(exc).__name__}: {exc}"),
                    )
                    if dead_lettered:
                        LOGGER.error("Telegram update %s exhausted its retry budget", update_id)
                        await self._notify_dead_letter(telegram, update, permanent=False)
                    else:
                        # Do not retry this row again if enough work in other chats
                        # makes its delay expire before this same drain returns.
                        blocked_keys.add(ordering_key)

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
        chunks = split_for_telegram(text)
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
