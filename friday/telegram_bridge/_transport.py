"""Telegram bridge: the long-polling loops, the outbound queue and signed backend calls.

Moved verbatim out of the single 1670-line module: same names, signatures and
bodies. Mixed back into ``TelegramBridge``, so every sibling call resolves as
before and nothing outside the package moved.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
from contextlib import suppress
from typing import TYPE_CHECKING

from friday.archive_passwords import bounded_archive_password, strip_archive_password_directives
from friday.telegram_bridge._base import (
    ALLOWED_UPDATE_KINDS,
    API_BASE,
    BACKOFF_MAX,
    BATCH_SIZE,
    BOT_COMMANDS,
    CALLBACK_TARGET_RE,
    LOGGER,
    MAX_CONCURRENT_UPDATES,
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
    asyncio,
    httpx,
    install_secret_redaction,
    json,
    sign_bridge_request,
    split_for_telegram,
    time,
    uuid,
)
from friday.telegram_bridge._markup import to_telegram_html
from friday.telegram_bridge._queue import _UpdateInbox
from friday.telegram_bridge._status import (
    TelegramStatusMessageManager,
    render_engineer_status,
)

# Long polling normally returns within ``POLL_TIMEOUT`` and even a failed round
# sleeps for no more than ``BACKOFF_MAX``.  A substantially larger silence means
# the coroutine is wedged inside a socket/backend transition rather than merely
# waiting for Telegram.  The exception intentionally escapes ``run`` so the
# already configured systemd ``Restart=on-failure`` creates fresh HTTP clients.
_POLL_WATCHDOG_INTERVAL_SEC = 15.0
_POLL_WATCHDOG_STALE_SEC = max(180.0, POLL_TIMEOUT + BACKOFF_MAX + 60.0)
_TRANSITION_JOURNAL_TIMEOUT_SEC = 5.0
_ALBUM_SETTLE_SEC = 1.0
_ALBUM_MAX_WAIT_SEC = 5.0
_ALBUM_MAX_ITEMS = 10
_DELIVERY_UNCERTAINTY_NOTICE = (
    "доставка не подтверждена, не дублирую; повторите запрос если фрагмент не пришёл"
)
_ENGINEER_TERMINAL_NOTIFICATION_KIND = "engineer_command_terminal"
_ENGINEER_TERMINAL_TEXT_NOTIFICATION_KIND = "engineer_command_terminal_text"
_ENGINEER_PROGRESS_NOTIFICATION_KIND = "engineer_command_progress"
_ENGINEER_STATUS_SCHEMA = "friday.telegram-status.v1"
_ENGINEER_TERMINAL_REVISION = (1 << 63) - 1
_ENGINEER_PROGRESS_REVISIONS = frozenset({60, 300, 900, 1800})
_ARCHIVE_DOCUMENT_SUFFIXES = (".zip", ".rar", ".7z")
_ARCHIVE_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/zip",
        "application/x-7z-compressed",
        "application/vnd.rar",
        "application/x-rar-compressed",
    }
)
_ARCHIVE_PASSWORD_REPLY_CUE_RE = re.compile(
    r"^[ \t]*(?:(?:это|вот)[ \t]+)?(?:пароль|password)"
    r"(?:[ \t]+(?:к|для)[ \t]+архив(?:а|у)?)?[ \t]*[.!?]?[ \t]*$",
    re.IGNORECASE,
)
_PRESENTATION_PASSWORD_QUOTES = {
    '"': '"',
    "'": "'",
    "«": "»",
    "“": "”",
    "‘": "’",
    "„": "“",
}


def _notification_ack_states(payload: dict[str, Any]) -> dict[str, set[str]] | None:
    """Parse proof-bearing ACK state ids; malformed evidence is no evidence."""

    raw = payload.get("state_ids")
    if not isinstance(raw, dict):
        return None
    states: dict[str, set[str]] = {}
    seen: set[str] = set()
    for status in (
        "sent",
        "failed",
        "uncertain",
        "pending",
        "dismissed",
        "missing",
        "unconfirmed",
    ):
        values = raw.get(status)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            return None
        current = set(values)
        if seen.intersection(current):
            return None
        seen.update(current)
        states[status] = current
    return states


def _engineer_terminal_envelope(
    item: dict[str, Any],
    *,
    chat_id: int,
    max_document_bytes: int,
) -> dict[str, Any] | None:
    """Validate and bind one content-free terminal artifact envelope."""

    notification_id = item.get("id")
    dedup_key = item.get("dedup_key")
    caption = item.get("caption")
    artifact = item.get("artifact")
    item_shape = {"id", "chat_id", "kind", "dedup_key", "caption", "artifact"}
    if (
        set(item) not in {frozenset(item_shape), frozenset(item_shape | {"status_update"})}
        or not isinstance(notification_id, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", notification_id) is None
        or item.get("kind") != _ENGINEER_TERMINAL_NOTIFICATION_KIND
        or item.get("chat_id") != str(chat_id)
        or not isinstance(dedup_key, str)
        or not dedup_key
        or len(dedup_key) > 512
        or not dedup_key.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in dedup_key)
        or not isinstance(caption, str)
        or not caption
        or split_for_telegram(caption, limit=1024) != [caption]
        or not isinstance(artifact, dict)
        or set(artifact) != {"filename", "mime_type", "size_bytes", "sha256", "path"}
    ):
        return None
    filename = artifact.get("filename")
    mime_type = artifact.get("mime_type")
    size_bytes = artifact.get("size_bytes")
    digest = artifact.get("sha256")
    path = artifact.get("path")
    if (
        not isinstance(filename, str)
        or not filename
        or filename != filename.strip()
        or len(filename) > 128
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or not isinstance(mime_type, str)
        or re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime_type) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= max(1, int(max_document_bytes))
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or path != f"/api/notifications/{notification_id}/artifact"
    ):
        return None
    identity = {
        "artifact": {
            "filename": filename,
            "mime_type": mime_type,
            "path": path,
            "sha256": digest,
            "size_bytes": size_bytes,
        },
        "caption": caption,
        "chat_id": str(chat_id),
        "dedup_key": dedup_key,
        "kind": _ENGINEER_TERMINAL_NOTIFICATION_KIND,
        "notification_id": notification_id,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    identity["fence_key"] = f"document:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return identity


def _engineer_status_update(item: dict[str, Any], *, chat_id: int) -> dict[str, Any] | None:
    """Validate content-free facts; never infer a status identity from prose."""

    raw = item.get("status_update")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Engineer status update is not an object")
    operation_id = raw.get("operation_id")
    if (
        raw.get("schema") != _ENGINEER_STATUS_SCHEMA
        or not isinstance(operation_id, str)
        or re.fullmatch(r"engineer:[0-9a-f]{32}", operation_id) is None
        or str(item.get("chat_id") or "") != str(chat_id)
        or isinstance(raw.get("revision"), bool)
        or not isinstance(raw.get("revision"), int)
        or not isinstance(raw.get("terminal"), bool)
    ):
        raise ValueError("Engineer status update is invalid")
    job_id = operation_id.removeprefix("engineer:")
    kind = str(item.get("kind") or "")
    dedup_key = str(item.get("dedup_key") or "")
    if kind == _ENGINEER_PROGRESS_NOTIFICATION_KIND:
        if (
            set(raw)
            != {
                "schema",
                "operation_id",
                "revision",
                "terminal",
                "stage",
                "elapsed_sec",
                "timeout_sec",
                "remaining_sec",
                "stdout_bytes",
                "stderr_bytes",
                "output_activity",
            }
            or raw["terminal"] is not False
            or raw.get("stage") != "command_running"
            or raw["revision"] not in _ENGINEER_PROGRESS_REVISIONS
            or dedup_key != f"engineer-progress:v1:{job_id}:{raw['revision']}"
            or any(
                isinstance(raw.get(field), bool) or not isinstance(raw.get(field), int) or int(raw[field]) < 0
                for field in ("elapsed_sec", "timeout_sec", "stdout_bytes", "stderr_bytes")
            )
            or int(raw["elapsed_sec"]) < int(raw["revision"])
            or not isinstance(raw.get("output_activity"), bool)
        ):
            raise ValueError("Engineer progress status update is invalid")
        timeout_sec = int(raw["timeout_sec"])
        remaining_sec = raw.get("remaining_sec")
        if (timeout_sec == 0 and remaining_sec is not None) or (
            timeout_sec > 0
            and (
                isinstance(remaining_sec, bool)
                or not isinstance(remaining_sec, int)
                or remaining_sec != max(0, timeout_sec - int(raw["elapsed_sec"]))
            )
        ):
            raise ValueError("Engineer progress deadline is invalid")
        return dict(raw)
    if kind in {
        _ENGINEER_TERMINAL_NOTIFICATION_KIND,
        _ENGINEER_TERMINAL_TEXT_NOTIFICATION_KIND,
    }:
        lane = "archive" if kind == _ENGINEER_TERMINAL_NOTIFICATION_KIND else "text"
        if (
            set(raw) != {"schema", "operation_id", "revision", "terminal", "stage"}
            or raw["terminal"] is not True
            or raw["revision"] != _ENGINEER_TERMINAL_REVISION
            or raw.get("stage") not in {"completed", "failed", "cancelled", "timeout"}
            or re.fullmatch(
                rf"engineer-terminal:{lane}:{job_id}:[0-9a-f]{{64}}",
                dedup_key,
            )
            is None
        ):
            raise ValueError("Engineer terminal status update is invalid")
        return dict(raw)
    raise ValueError("Engineer status kind is invalid")


async def _publish_engineer_status(
    bridge: BridgeShared,
    telegram: httpx.AsyncClient,
    chat_id: int,
    update: dict[str, Any],
    *,
    delivery_uncertain: bool = False,
) -> None:
    projected = dict(update)
    if delivery_uncertain:
        projected["stage"] = "delivery_uncertain"
    await bridge._status_messages.publish(
        telegram,
        chat_id,
        str(projected["operation_id"]),
        int(projected["revision"]),
        render_engineer_status(projected),
        terminal=bool(projected["terminal"]),
        create=True,
    )


async def _fetch_engineer_terminal_artifact(
    bridge: Any,
    backend: httpx.AsyncClient,
    *,
    signer_chat: str,
    artifact: dict[str, Any],
) -> tuple[str, bytes | None]:
    """Fetch separately authorized bytes as ``ready``, ``failed`` or ``deferred``."""

    path = str(artifact["path"])
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signature = sign_bridge_request(
        bridge.config.bridge_secret,
        timestamp=timestamp,
        method="GET",
        path=path,
        external_user_id=signer_chat,
        chat_id=signer_chat,
        nonce=nonce,
        body=b"",
    )
    try:
        response = await backend.request(
            "GET",
            f"{bridge._backend_url}{path}",
            headers={
                "X-Friday-Timestamp": str(timestamp),
                "X-Friday-User": signer_chat,
                "X-Friday-Chat": signer_chat,
                "X-Friday-Nonce": nonce,
                "X-Friday-Signature": signature,
            },
        )
    except Exception:
        return "deferred", None
    if 400 <= int(response.status_code) < 500:
        return "failed", None
    if not 200 <= int(response.status_code) < 300:
        return "deferred", None
    content = bytes(response.content)
    if len(content) != artifact["size_bytes"] or not hmac.compare_digest(
        hashlib.sha256(content).hexdigest(),
        artifact["sha256"],
    ):
        return "failed", None
    return "ready", content


def _telegram_document_outcome(response: httpx.Response) -> str:
    """Classify Telegram proof: accepted, explicitly rejected, or ambiguous."""

    try:
        payload = response.json()
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return "uncertain"
    if not isinstance(payload, dict):
        return "uncertain"
    status = int(response.status_code)
    if 400 <= status < 500:
        error_code = payload.get("error_code")
        description = payload.get("description")
        return (
            "failed"
            if payload.get("ok") is False
            and isinstance(error_code, int)
            and not isinstance(error_code, bool)
            and error_code == status
            and isinstance(description, str)
            and bool(description)
            else "uncertain"
        )
    if not 200 <= status < 300 or payload.get("ok") is not True:
        return "uncertain"
    result = payload.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return "uncertain"
    return "sent"


async def _deliver_engineer_terminal_document(
    bridge: Any,
    telegram: httpx.AsyncClient,
    backend: httpx.AsyncClient,
    *,
    signer_chat: str,
    envelope: dict[str, Any],
) -> str:
    """Deliver one captioned archive as sent/failed/uncertain/deferred."""

    notification_id = str(envelope["notification_id"])
    fence_key = str(envelope["fence_key"])
    states = bridge._inbox.notification_delivery_part_states(notification_id)
    if any(key.startswith("document:") and key != fence_key for key in states):
        return "uncertain"
    if states.get(fence_key) == "uncertain":
        return "uncertain"
    if states.get(fence_key) == "confirmed":
        return "sent"

    artifact = envelope["artifact"]
    fetch_outcome, content = await _fetch_engineer_terminal_artifact(
        bridge,
        backend,
        signer_chat=signer_chat,
        artifact=artifact,
    )
    if fetch_outcome != "ready" or content is None:
        return fetch_outcome
    state = bridge._inbox.begin_notification_part_delivery(notification_id, fence_key)
    if state != "armed":
        return "sent" if state == "confirmed" else "uncertain"
    try:
        response = await telegram.post(
            f"{bridge._api_url}/sendDocument",
            data={"chat_id": str(envelope["chat_id"]), "caption": envelope["caption"]},
            files={
                "document": (
                    artifact["filename"],
                    content,
                    artifact["mime_type"],
                )
            },
        )
    except httpx.ConnectError:
        return (
            "failed"
            if bridge._inbox.reject_notification_part_delivery(notification_id, fence_key)
            else "uncertain"
        )
    except Exception:
        return "uncertain"
    outcome = _telegram_document_outcome(response)
    if outcome == "failed":
        return (
            "failed"
            if bridge._inbox.reject_notification_part_delivery(notification_id, fence_key)
            else "uncertain"
        )
    if outcome != "sent":
        return "uncertain"
    try:
        confirmed = bridge._inbox.confirm_notification_part_delivery(notification_id, fence_key)
    except Exception:
        return "uncertain"
    return "sent" if confirmed else "uncertain"


class _AlbumPermanentError(PermanentUpdateError):
    """An invalid group plus every durable row that belongs to it."""

    def __init__(self, message: str, rows: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.update_ids = [int(row["update_id"]) for row in rows]


def _message_text_field(message: dict[str, Any]) -> tuple[str, str]:
    if isinstance(message.get("text"), str):
        return "text", str(message.get("text") or "")
    if isinstance(message.get("caption"), str):
        return "caption", str(message.get("caption") or "")
    return "", ""


def _scrub_archive_password_directive(message: dict[str, Any]) -> tuple[str, str | None]:
    """Remove one explicit credential from a message before durable storage."""

    field, original = _message_text_field(message)
    if not field:
        return original, None
    safe_text, secret = strip_archive_password_directives(original)
    if secret is None:
        return original, None
    message[field] = safe_text
    # Telegram entity offsets refer to the pre-redaction text and must never be
    # used to reconstruct the removed credential.
    message.pop("entities", None)
    message.pop("caption_entities", None)
    return safe_text, secret


def _standalone_archive_password(value: str) -> str | None:
    """Accept a closed credential shape, never an ambiguous one-word utterance.

    Passwords containing spaces or plain words remain supported through the
    advertised ``пароль: …`` form. Matching presentation quotes are also an
    explicit credential boundary. An unquoted standalone value must contain a
    digit or an internal credential separator; terminal sentence punctuation
    alone is not a credential signal. This keeps ordinary prose such as
    ``Почему?`` or ``стоп.`` out of the pending-archive channel while retaining
    the historical one-token password-followup form used by the bridge.
    """

    safe = bounded_archive_password(value)
    if safe is None:
        return None
    core = value.strip(" \t")
    if not core:
        return None
    if len(core) >= 2 and _PRESENTATION_PASSWORD_QUOTES.get(core[0]) == core[-1]:
        return safe
    if any(character.isspace() for character in core):
        return None
    without_sentence_punctuation = core.rstrip(".!?,;:…")
    if without_sentence_punctuation and all(
        character.isalpha() for character in without_sentence_punctuation
    ):
        return None
    return safe


class _LazyUpdateInbox:
    """Delay every SQLite touch until the bridge owns its process lease."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._instance: _UpdateInbox | None = None

    def _opened(self) -> _UpdateInbox:
        if self._instance is None:
            self._instance = _UpdateInbox(self._path)
        return self._instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self._opened(), name)

    def close(self) -> None:
        instance = self._instance
        self._instance = None
        if instance is not None:
            instance.close()


class TransportMixin(BridgeShared):
    if TYPE_CHECKING:

        @staticmethod
        def _select_media(
            message: dict[str, Any], update: dict[str, Any]
        ) -> tuple[dict[str, Any] | None, str, str, str]: ...

    def __init__(self, config: TelegramConfig) -> None:
        config.validate()
        self.config = config
        self._running = False
        # Работа, запущенная и ещё не законченная, — по одной задаче на чат.
        # Ключ тот же `ordering_key`, которым хранилище держит FIFO внутри чата:
        # пока задача этого чата в полёте, следующая его строка не берётся, и
        # порядок сохраняется без прежней сериализации ВСЕХ чатов подряд.
        self._inflight: dict[str, asyncio.Task[None]] = {}
        # Подписи альбомов по `media_group_id`. Ограничены по числу: группа живёт
        # секунды, а словарь без потолка рос бы всю жизнь процесса.
        self._album_captions: dict[str, str] = {}
        # Passwords live only until their one sanitized durable update finishes.
        # The queue and the pending-media table never receive this mapping.
        self._archive_passwords: dict[int, str] = {}
        # Раздача прекращается на остановке, но уже начатое доводится до конца:
        # брошенная задача — это обновление, снятое с очереди и не отвеченное.
        self._stopping = False
        inbox_path = Path(config.inbox_db_path)
        self._lease = ProcessLease(
            inbox_path.with_name(f"{inbox_path.name}.lock"),
            protocol="friday.telegram-bridge.v1",
        )
        # Production opens/migrates the queue only AFTER acquiring the process
        # lease in ``run``.  The lazy property preserves the small direct bridge
        # fixtures without letting a losing second production process map the
        # live WAL before it discovers that another bridge owns it.
        self._inbox = _LazyUpdateInbox(config.inbox_db_path)
        self._offset = 0
        self._api_url = f"{API_BASE}/bot{config.bot_token}"
        self._file_url = f"{API_BASE}/file/bot{config.bot_token}"
        self._status_messages = TelegramStatusMessageManager(self._inbox, api_url=self._api_url)
        # The API URL CONTAINS the bot token, and httpx puts the URL in the text
        # of every HTTPStatusError. Those strings were stored verbatim on the
        # queue row and then surfaced by `jericho doctor`, `jericho status
        # --json` and `GET /api/admin/diagnostics` — the credential printed by
        # the health check.
        #
        # Что защищает СЕГОДНЯ — два конца, и ни один из них не редактор: в
        # очередь пишется только ИМЯ КЛАССА исключения (`mark_failure`,
        # `mark_dead_letter`), а диагностика вдобавок чистит то, что читает, —
        # на случай строки, записанной прежней сборкой. Здесь же до 0.185.0 жил
        # третий, никем не вызываемый: `self._redact` присваивался и не
        # использовался ни разу. Проба у него была, потребителя не было —
        # ровно тот случай, когда механизм проверен, а подключение нет.
        self._backend_url = config.backend_url.rstrip("/")
        # Last known failing/healthy state per loop, for transition detection.
        self._loop_failing: dict[str, bool] = {}
        self._warned_no_signer = False
        # Своё имя в Telegram: узнаётся при подъёме через `getMe`. Пустое —
        # значит обращения по `@имени` мост не узнаёт и в группе отвечает
        # только на команды и на ответы своим сообщениям.
        self._bot_username = ""
        # Сторож backend: мост — первый, кто узнаёт о его смерти, и единственный,
        # кто может об этом сказать (sentinel живёт ВНУТРИ backend и молчит с ним).
        self._backend_down_since = 0.0
        self._backend_down_warned_at = 0.0
        self._poll_heartbeat_at = time.monotonic()
        # Activity is not success.  A broken keep-alive can fail quickly,
        # enter backoff and reset the heartbeat forever while Telegram keeps
        # updates undelivered.  Track the last completed getUpdates round
        # separately so systemd can replace the HTTP client after a sustained
        # transport failure.
        self._poll_success_at = self._poll_heartbeat_at

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
            raise
        try:
            self._offset = self._inbox.get_offset()
        except BaseException:
            self._inbox.close()
            self._lease.release()
            raise
        self._running = True
        timeout = httpx.Timeout(POLL_TIMEOUT + 10.0, connect=15.0)
        # Start from httpx's standard public root bundle with environment overrides disabled,
        # then add the operator's private CA when this backend uses a self-signed or
        # locally issued certificate.  Hostname verification remains mandatory.
        backend_ssl_context = httpx.create_ssl_context(verify=True, trust_env=False)
        if self.config.backend_ca_file:
            backend_ssl_context.load_verify_locations(cafile=self.config.backend_ca_file)
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
                    verify=backend_ssl_context,
                ) as backend,
            ):
                LOGGER.info(
                    "Telegram bridge started at offset %d%s",
                    self._offset,
                    " via configured proxy" if self.config.telegram_proxy else "",
                )
                await self._learn_own_username(telegram)
                await self._register_commands(telegram)
                # Inbound polling and outbound push run concurrently; a crash in
                # one loop must not take down the other, so each supervises itself.
                await asyncio.gather(
                    self._poll_loop(telegram, backend),
                    self._outbound_loop(telegram, backend),
                    self._poll_watchdog(),
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
        """Зафиксировать сбой без содержимого exception и traceback.

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

        Даже первый traceback может содержать URL Telegram, query или текст
        ответа. Для диагностики остаются имя цикла и класс исключения.
        """
        if self._loop_failing.get(loop_name) is not True:
            LOGGER.error("Telegram bridge %s loop failed: %s", loop_name, type(error).__name__)
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
            await asyncio.wait_for(
                self._backend_json(
                    backend,
                    "POST",
                    "/api/events",
                    {
                        "event_type": f"bridge.{loop_name}_{'failed' if failing else 'recovered'}",
                        "payload": payload,
                    },
                    signer,
                    signer,
                ),
                timeout=_TRANSITION_JOURNAL_TIMEOUT_SEC,
            )
        except Exception as exc:
            LOGGER.debug("Could not journal bridge %s transition (%s)", loop_name, type(exc).__name__)

    async def _learn_own_username(self, telegram: httpx.AsyncClient) -> None:
        """Своё имя в Telegram — чтобы узнавать обращение к себе в группе.

        Имя спрашивается У TELEGRAM, а не берётся из настройки: настройка была бы
        четвёртым местом, где живёт одна и та же правда (токен, id бота внутри
        токена, меню команд), и разошлась бы при первом же переименовании бота.

        Best-effort: без имени мост работает как раньше — в группе он тогда не
        узнаёт обращения по `@имени` и отвечает только на команды и ответы на свои
        сообщения. Молчаливого «отвечаю всем подряд» при этом не возникает.
        """
        try:
            response = await telegram.post(f"{self._api_url}/getMe", json={})
            response.raise_for_status()
            body = response.json()
            result = body.get("result") if isinstance(body, dict) else None
            username = str((result or {}).get("username") or "").strip()
        except Exception as exc:
            LOGGER.warning("Telegram getMe failed (non-fatal, %s)", type(exc).__name__)
            return
        if username:
            self._bot_username = username
            LOGGER.info("Telegram bot username: @%s", username)

    async def _register_commands(self, telegram: httpx.AsyncClient) -> None:
        """Register the command menu once so Telegram shows '/' autocomplete.

        The command surface is otherwise discoverable only by remembering /help.
        Best-effort: a failure here must never stop the bridge from starting.
        """
        hidden: set[str] = set()
        if not self.config.obsidian_enabled:
            hidden.update({"obsidian", "obsidian_alias"})
        if not self.config.engineer_mode_enabled:
            hidden.add("engineer")
        commands = tuple(item for item in BOT_COMMANDS if item[0] not in hidden)
        payload = {"commands": [{"command": name, "description": desc} for name, desc in commands]}
        try:
            response = await telegram.post(f"{self._api_url}/setMyCommands", json=payload)
            response.raise_for_status()
        except Exception as exc:
            LOGGER.warning("Telegram setMyCommands failed (non-fatal, %s)", type(exc).__name__)

    async def _poll_loop(self, telegram: httpx.AsyncClient, backend: httpx.AsyncClient) -> None:
        backoff = 1.0
        while self._running:
            self._poll_heartbeat_at = time.monotonic()
            try:
                await self._drain_inbox(telegram, backend)
                updates = await self._get_updates(telegram)
                self._poll_heartbeat_at = time.monotonic()
                self._poll_success_at = self._poll_heartbeat_at
                for update in updates:
                    safe_update = self._sanitize_update_before_store(update)
                    stored = self._inbox.store(safe_update)
                    if not stored:
                        self._archive_passwords.pop(int(update.get("update_id") or -1), None)
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    self._inbox.set_offset(self._offset)
                if updates:
                    await self._drain_inbox(telegram, backend)
                backoff = 1.0
                await self._journal_transition(backend, "poll", failing=False)
                self._poll_heartbeat_at = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_loop_failure("poll", exc)
                await self._journal_transition(backend, "poll", failing=True, error=exc)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX, backoff * 2)

    @staticmethod
    def _archive_document_descriptor(message: dict[str, Any]) -> dict[str, Any] | None:
        document = message.get("document")
        if not isinstance(document, dict):
            return None
        filename = str(document.get("file_name") or "").casefold()
        mime_type = str(document.get("mime_type") or "").split(";", 1)[0].strip().casefold()
        if (
            not filename.endswith(_ARCHIVE_DOCUMENT_SUFFIXES)
            and mime_type not in _ARCHIVE_DOCUMENT_MIME_TYPES
        ):
            return None
        return document

    @staticmethod
    def _strip_archive_password_directives(text: str) -> tuple[str, str | None]:
        return strip_archive_password_directives(text)

    @staticmethod
    def _bounded_ephemeral_archive_password(value: str | None) -> str | None:
        return bounded_archive_password(value)

    def _sanitize_update_before_store(self, update: dict[str, Any]) -> dict[str, Any]:
        """Strip archive credentials before the first durable Telegram write."""

        safe_update = copy.deepcopy(update)
        update_id = int(safe_update.get("update_id") or -1)
        message = safe_update.get("message")
        if update_id < 0 or not isinstance(message, dict):
            return safe_update
        raw_chat = message.get("chat")
        raw_user = message.get("from")
        chat_id = int(raw_chat.get("id") or 0) if isinstance(raw_chat, dict) else 0
        user_id = int(raw_user.get("id") or 0) if isinstance(raw_user, dict) else 0
        if not chat_id or not user_id:
            return safe_update

        text_field, original_text = _message_text_field(message)
        safe_text, explicit_secret = _scrub_archive_password_directive(message)
        descriptor = self._archive_document_descriptor(message)
        current_media, _filename, _mime_type, _media_kind = self._select_media(message, safe_update)
        replied_to = message.get("reply_to_message")
        replied_message: dict[str, Any] | None = replied_to if isinstance(replied_to, dict) else None
        replied_original_text = ""
        replied_explicit_secret: str | None = None
        replied_archive = None
        if replied_message is not None:
            _reply_field, replied_original_text = _message_text_field(replied_message)
            _safe_reply, replied_explicit_secret = _scrub_archive_password_directive(replied_message)
            replied_archive = self._archive_document_descriptor(replied_message)
        pending = self._inbox.archive_password_challenge(chat_id, user_id)
        followup = False
        candidate_secret: str | None = None

        if descriptor is not None:
            # A current archive owns its own credential and can never be
            # replaced by an older pending challenge.
            candidate_secret = explicit_secret
        elif current_media is not None:
            # Any current Telegram media wins over pending archive state.  In
            # particular, an ODT/PDF with a caption is a new upload, not a
            # password for the previous RAR.
            candidate_secret = None
        elif replied_archive is not None:
            # The archive and its caption are one Telegram object.  A password
            # directive in that caption is valid for an exact structural reply,
            # but is removed from the durable copy and from backend `reply_to`.
            candidate_secret = explicit_secret or replied_explicit_secret
        elif pending is not None:
            candidate_secret = explicit_secret or replied_explicit_secret
            if candidate_secret is None:
                candidate_secret = _standalone_archive_password(original_text)
                if candidate_secret is not None and text_field:
                    message[text_field] = ""
                    message.pop("entities", None)
                    message.pop("caption_entities", None)
                    safe_text = ""
            if (
                candidate_secret is None
                and replied_message is not None
                and _ARCHIVE_PASSWORD_REPLY_CUE_RE.fullmatch(original_text)
            ):
                # `Это пароль` may point at a password-only Telegram message.
                # The explicit cue and structural reply make the role closed;
                # the quoted credential itself is scrubbed before SQLite.
                candidate_secret = self._bounded_ephemeral_archive_password(replied_original_text)
                if candidate_secret is not None:
                    reply_field, _reply_text = _message_text_field(replied_message)
                    if reply_field:
                        replied_message[reply_field] = ""
                        replied_message.pop("entities", None)
                        replied_message.pop("caption_entities", None)
            followup = candidate_secret is not None
        secret = self._bounded_ephemeral_archive_password(candidate_secret)

        if explicit_secret is not None or replied_explicit_secret is not None or followup:
            # Text entities carry offsets into the removed password.  They are
            # content-free but useless after rewriting and must not accidentally
            # make a later formatter reconstruct the old caption.
            if text_field and explicit_secret is not None:
                message[text_field] = safe_text
                message.pop("entities", None)
                message.pop("caption_entities", None)
            safe_update["friday_archive_password_followup"] = followup
            safe_update["friday_archive_password_supplied"] = secret is not None
        if secret is not None:
            self._archive_passwords[update_id] = secret
            if descriptor is not None and secret in str(descriptor.get("file_name") or ""):
                descriptor["file_name"] = "protected-archive.bin"

        return safe_update

    async def _poll_watchdog(self) -> None:
        """Crash a formally live bridge whose Telegram poll made no progress.

        HTTP timeouts cover ordinary network failures, while this guard covers
        the rarer half-dead state: the process and event loop still exist, but a
        coroutine never returns.  Durable inbox rows and the persisted offset
        make a process restart lossless and preferable to silent bot downtime.
        """

        while self._running:
            await asyncio.sleep(_POLL_WATCHDOG_INTERVAL_SEC)
            now = time.monotonic()
            heartbeat_age = now - self._poll_heartbeat_at
            success_age = now - self._poll_success_at
            if heartbeat_age <= _POLL_WATCHDOG_STALE_SEC and success_age <= _POLL_WATCHDOG_STALE_SEC:
                continue
            LOGGER.error(
                "Telegram bridge poll watchdog expired (heartbeat %.0fs, success %.0fs)",
                heartbeat_age,
                success_age,
            )
            raise RuntimeError("telegram_poll_watchdog_expired")

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
                        f"⚠️ Backend Friday не отвечает уже ~{minutes} мин. "
                        "Бот принимает сообщения, но обработка стоит; они дойдут после "
                        "восстановления. Проверьте процесс jericho up на хосте."
                    ),
                },
            )
            answer.raise_for_status()
            self._backend_down_warned_at = now
        except Exception as exc:  # noqa: BLE001 - тревога не должна ронять цикл
            LOGGER.warning("Could not warn the owner about the backend outage (%s)", type(exc).__name__)

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
                    "text": "✅ Backend Friday снова отвечает; накопившиеся сообщения обрабатываются.",
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
                    "Add the owner's private chat id to FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS "
                    "or FRIDAY_TELEGRAM_OWNER_CHAT_IDS."
                )
            return
        data = await self._backend_json(
            backend,
            "GET",
            "/api/notifications/pending?limit=20&status_messages=1",
            None,
            signer_chat,
            signer_chat,
        )
        raw_retired = data.get("retired")
        retired: list[str] = []
        if isinstance(raw_retired, list):
            retired = [value for value in raw_retired[:100] if isinstance(value, str) and value]
        raw_items = data.get("items")
        items: list[Any] = raw_items if isinstance(raw_items, list) else []
        terminal_outcomes = self._inbox.notification_delivery_outcomes(limit=100)
        # A process may die after the pre-write fence (or even after its success
        # confirmation) but before persisting the separate ACK outcome. Promote
        # every such orphan to a durable outcome so a later revoke/discard that
        # hides the backend row cannot strand the fence forever.
        for notification_id, inferred in self._inbox.notification_delivery_orphan_outcomes(limit=100).items():
            self._inbox.remember_notification_delivery_outcome(notification_id, inferred)
            terminal_outcomes[notification_id] = inferred
        # A retirement can race a previously delivered document whose ACK was
        # lost.  Its local fence is the only remaining evidence of sent versus
        # uncertain, so reconcile that evidence before cleanup.  Retired ids
        # without any strict local state are safe to discard immediately.
        retired_without_proof: list[str] = []
        for notification_id in retired:
            if notification_id in terminal_outcomes:
                continue
            exact_outcome = self._inbox.notification_delivery_outcome(notification_id)
            if exact_outcome is None:
                parts = self._inbox.notification_delivery_part_states(notification_id)
                if parts:
                    exact_outcome = (
                        "sent" if all(state == "confirmed" for state in parts.values()) else "uncertain"
                    )
                    self._inbox.remember_notification_delivery_outcome(
                        notification_id,
                        exact_outcome,
                    )
            if exact_outcome is None:
                retired_without_proof.append(notification_id)
            else:
                terminal_outcomes[notification_id] = exact_outcome
        self._inbox.forget_notification_delivery_parts(retired_without_proof)
        sent: list[str] = [
            notification_id for notification_id, outcome in terminal_outcomes.items() if outcome == "sent"
        ]
        failed: list[str] = []
        uncertain: list[str] = [
            notification_id
            for notification_id, outcome in terminal_outcomes.items()
            if outcome == "uncertain"
        ]
        # Строки, которые уже ушли человеку, но чьё подтверждение до бэкенда не
        # доехало: он честно предлагает их снова, потому что для него они всё ещё
        # `pending`. Отправить их второй раз — и человек получит дубль пачки.
        already_delivered = self._inbox.delivered_notification_ids()
        for item in items:
            if not isinstance(item, dict):
                continue
            notif_id = str(item.get("id") or "")
            chat_raw = str(item.get("chat_id") or "")
            body = str(item.get("body") or "")
            kind = str(item.get("kind") or "")
            if not notif_id or (kind != _ENGINEER_TERMINAL_NOTIFICATION_KIND and not body):
                continue
            if notif_id in terminal_outcomes:
                # Reconcile only the exact envelope that reached Telegram. A
                # backend row changing while its ACK response is lost must not
                # let the old successful send bless new chat/caption/artifact
                # bytes under the reused notification id.
                try:
                    outcome_chat_id = int(chat_raw)
                except ValueError:
                    outcome_chat_id = 0
                outcome_envelope = (
                    _engineer_terminal_envelope(
                        item,
                        chat_id=outcome_chat_id,
                        max_document_bytes=self.config.max_document_bytes,
                    )
                    if kind == _ENGINEER_TERMINAL_NOTIFICATION_KIND and outcome_chat_id
                    else None
                )
                outcome_states = self._inbox.notification_delivery_part_states(notif_id)
                exact_fence = (
                    str(outcome_envelope.get("fence_key") or "") if outcome_envelope is not None else ""
                )
                if not exact_fence or exact_fence not in outcome_states:
                    self._inbox.remember_notification_delivery_outcome(notif_id, "uncertain")
                    terminal_outcomes[notif_id] = "uncertain"
                    sent = [value for value in sent if value != notif_id]
                    if notif_id not in uncertain:
                        uncertain.append(notif_id)
                if item.get("status_update") is not None and outcome_chat_id:
                    if not self._may_message_chat(outcome_chat_id):
                        # The durable artifact outcome is not renewed authority
                        # for a later status create/edit. Keep the exact outcome
                        # for reconciliation if this chat is admitted again.
                        LOGGER.warning("Engineer reconciled status chat is no longer admitted")
                        sent = [value for value in sent if value != notif_id]
                        uncertain = [value for value in uncertain if value != notif_id]
                        continue
                    try:
                        outcome_status = _engineer_status_update(item, chat_id=outcome_chat_id)
                        if outcome_status is not None:
                            await _publish_engineer_status(
                                self,
                                telegram,
                                outcome_chat_id,
                                outcome_status,
                                delivery_uncertain=terminal_outcomes[notif_id] == "uncertain",
                            )
                    except Exception as exc:
                        # The artifact outcome is already durable, so do not send
                        # it again and do not ACK it yet. The next drain retries
                        # only this idempotent status edit.
                        LOGGER.warning(
                            "Engineer reconciled status delivery failed (%s)",
                            type(exc).__name__,
                        )
                        sent = [value for value in sent if value != notif_id]
                        uncertain = [value for value in uncertain if value != notif_id]
                # Exact prior outcome or drift-downgraded uncertainty: ACK it,
                # never send either envelope again.
                continue
            if notif_id in already_delivered:
                # Не доставка текста, а повтор подтверждения. A structured
                # terminal status may have failed after that text arrived; retry
                # only its idempotent edit before ACKing the body.
                if item.get("status_update") is not None:
                    try:
                        delivered_chat_id = int(chat_raw)
                        if not self._may_message_chat(delivered_chat_id):
                            raise ValueError("chat no longer admitted")
                        delivered_status = _engineer_status_update(
                            item,
                            chat_id=delivered_chat_id,
                        )
                        if delivered_status is not None and bool(delivered_status["terminal"]):
                            await _publish_engineer_status(
                                self,
                                telegram,
                                delivered_chat_id,
                                delivered_status,
                            )
                    except Exception as exc:
                        LOGGER.warning(
                            "Engineer delivered status retry failed (%s)",
                            type(exc).__name__,
                        )
                        continue
                sent.append(notif_id)
                continue
            # Deny-by-default re-check at the send edge: the bot token can reach
            # any chat, so an outbound message must target an allowed chat only
            # — the static allowlist, or a chat open registration already
            # admitted (proactive organs would otherwise never reach a
            # self-registered account: their push always routes through here).
            try:
                candidate = int(chat_raw)
                if not self._may_message_chat(candidate):
                    failed.append(notif_id)
                    continue
                chat_id = candidate
            except ValueError:
                failed.append(notif_id)
                continue
            try:
                status_update = _engineer_status_update(item, chat_id=chat_id)
            except ValueError:
                LOGGER.error("Invalid Engineer status carrier; refusing delivery")
                failed.append(notif_id)
                continue
            if kind == _ENGINEER_PROGRESS_NOTIFICATION_KIND and status_update is not None:
                try:
                    await _publish_engineer_status(self, telegram, chat_id, status_update)
                    self._inbox.remember_delivered_notification(notif_id)
                    sent.append(notif_id)
                except Exception as exc:
                    LOGGER.warning("Engineer progress status delivery failed (%s)", type(exc).__name__)
                    failed.append(notif_id)
                await asyncio.sleep(0.05)
                continue
            # Заявка на подтверждение приходит с кнопками решения: уведомление,
            # которое лишь СООБЩАЕТ о необходимости решить, заставляет человека
            # идти за ним в другую команду, и половина смысла проактивности теряется.
            markup = None
            dedup_key = str(item.get("dedup_key") or "")
            if kind == _ENGINEER_TERMINAL_NOTIFICATION_KIND:
                envelope = _engineer_terminal_envelope(
                    item,
                    chat_id=chat_id,
                    max_document_bytes=self.config.max_document_bytes,
                )
                if envelope is None:
                    LOGGER.error("Invalid Engineer terminal notification carrier; refusing delivery")
                    failed.append(notif_id)
                else:
                    outcome = await _deliver_engineer_terminal_document(
                        self,
                        telegram,
                        backend,
                        signer_chat=signer_chat,
                        envelope=envelope,
                    )
                    if outcome == "sent":
                        self._inbox.remember_notification_delivery_outcome(notif_id, "sent")
                        if status_update is not None:
                            try:
                                await _publish_engineer_status(self, telegram, chat_id, status_update)
                            except Exception as exc:
                                LOGGER.warning(
                                    "Engineer terminal status delivery failed (%s)",
                                    type(exc).__name__,
                                )
                                await asyncio.sleep(0.05)
                                continue
                        sent.append(notif_id)
                    elif outcome == "uncertain":
                        self._inbox.remember_notification_delivery_outcome(notif_id, "uncertain")
                        if status_update is not None:
                            try:
                                await _publish_engineer_status(
                                    self,
                                    telegram,
                                    chat_id,
                                    status_update,
                                    delivery_uncertain=True,
                                )
                            except Exception as exc:
                                LOGGER.warning(
                                    "Engineer uncertain status delivery failed (%s)",
                                    type(exc).__name__,
                                )
                                await asyncio.sleep(0.05)
                                continue
                        uncertain.append(notif_id)
                    elif outcome == "failed":
                        failed.append(notif_id)
                await asyncio.sleep(0.05)
                continue
            if kind == "approval" and dedup_key.startswith("approval:"):
                approval_id = dedup_key.split(":", 1)[1]
                if CALLBACK_TARGET_RE.fullmatch(approval_id):
                    markup = {
                        "inline_keyboard": [
                            [
                                {"text": "✓ Подтвердить", "callback_data": f"apr:yes:{approval_id}"},
                                {"text": "✕ Отклонить", "callback_data": f"apr:no:{approval_id}"},
                            ]
                        ]
                    }
            elif kind == "mission" and dedup_key.startswith("mission:"):
                # По той же причине, что и у заявки: миссия, ждущая запуска,
                # приходила бы текстом «откройте /missions» — то есть просьбой
                # сходить за решением в другую команду.
                #
                # Кнопки те же, что в списке миссий, и обработчик у них общий:
                # заводить второй путь к тому же действию значит однажды их
                # рассинхронизировать.
                mission_id = dedup_key.split(":", 1)[1]
                if CALLBACK_TARGET_RE.fullmatch(mission_id):
                    markup = {
                        "inline_keyboard": [
                            [
                                {"text": "▶ Запустить", "callback_data": f"mission:start:{mission_id}"},
                                {"text": "✕ Остановить", "callback_data": f"mission:stop:{mission_id}"},
                            ]
                        ]
                    }
            try:
                await self._send_message(telegram, chat_id, body, reply_markup=markup)
                # Запись ДО добавления в список: список умрёт вместе с процессом,
                # а человек сообщение уже получил.
                self._inbox.remember_delivered_notification(notif_id)
                if kind == _ENGINEER_TERMINAL_TEXT_NOTIFICATION_KIND and status_update is not None:
                    try:
                        await _publish_engineer_status(self, telegram, chat_id, status_update)
                    except Exception as exc:
                        LOGGER.warning(
                            "Engineer terminal text status delivery failed (%s)",
                            type(exc).__name__,
                        )
                        await asyncio.sleep(0.05)
                        continue
                sent.append(notif_id)
            except Exception as exc:
                LOGGER.warning("Outbound notification delivery failed (%s)", type(exc).__name__)
                failed.append(notif_id)
            # Gentle per-chat pacing to stay within Telegram send limits.
            await asyncio.sleep(0.05)
        if sent or failed or uncertain:
            sent = list(dict.fromkeys(sent))
            failed = list(dict.fromkeys(failed))
            uncertain = list(dict.fromkeys(uncertain))
            if uncertain:
                await self._ack_outbound(
                    backend,
                    signer_chat,
                    sent,
                    failed,
                    uncertain=uncertain,
                )
            else:
                # Preserve the historical call seam for ordinary notification
                # adapters; strict uncertainty is opt-in by kind only.
                await self._ack_outbound(backend, signer_chat, sent, failed)

    async def _ack_outbound(
        self,
        backend: httpx.AsyncClient,
        signer_chat: str,
        sent: list[str],
        failed: list[str],
        *,
        uncertain: list[str] | None = None,
    ) -> None:
        """Report the batch's outcome, retrying in place before giving up.

        Acking each message right after its send would keep the backend's view
        current message by message, and is NOT done: the bridge signs its service
        calls as the owner, so they count against `telegram:user:<owner>` — 30
        requests per minute by default, shared with the owner's own messages.
        Twenty acks per drain would spend that budget on bookkeeping and start
        429-ing real traffic. So one ack per batch, retried three times here.

        What a failed ack used to mean: the delivery state lived ONLY in the local
        `sent` list, so the whole batch — up to twenty messages — stayed `pending`
        on the backend and was delivered to the person AGAIN fifteen seconds later,
        and on every cycle until some ack landed.

        It no longer means that. The fact of delivery is written into the bridge's
        own durable queue at the moment of delivery
        (`remember_delivered_notification`), so the next drain recognises those ids
        in the backend's pending list and re-acks them instead of re-sending. A
        successful ack retires the local rows here — after that the backend is the
        one that remembers.
        """
        uncertain = list(uncertain or [])
        ack_payload: dict[str, Any] = {"sent": sent, "failed": failed}
        if uncertain:
            ack_payload["uncertain"] = uncertain
        requested = set([*sent, *failed, *uncertain])
        strict_requested = requested.intersection(self._inbox.notification_delivery_ids())
        last_error: Exception | None = None
        # A failed Telegram send is a counted attempt. Reposting an ACK whose
        # response was lost would count the same send twice, so failed batches
        # retry only after the next drain performs another real Telegram try.
        ack_attempts = 1 if failed else 3
        for attempt in range(ack_attempts):
            try:
                response = await self._backend_json(
                    backend,
                    "POST",
                    "/api/notifications/ack",
                    ack_payload,
                    signer_chat,
                    signer_chat,
                )
                states = _notification_ack_states(response)
                terminal: set[str] = set()
                if states is not None:
                    terminal = set().union(
                        states["sent"],
                        states["failed"],
                        states["uncertain"],
                        states["dismissed"],
                        states["missing"],
                    )
                if strict_requested:
                    if states is None:
                        raise RuntimeError("backend ACK carried no verifiable notification states")
                    reported = set().union(*states.values())
                    if strict_requested.difference(reported):
                        raise RuntimeError("backend ACK omitted strict notification state")
                    unresolved = strict_requested.intersection(states["unconfirmed"])
                    invalid_pending = strict_requested.intersection([*sent, *uncertain]).intersection(
                        states["pending"]
                    )
                    if unresolved or invalid_pending:
                        raise RuntimeError("backend ACK did not prove strict terminal state")
                if states is None:
                    # Compatibility with an older backend for ordinary text
                    # notifications only. Strict fences never trust echoed
                    # counts or the absence of proof-bearing state ids.
                    ordinary_sent = [value for value in sent if value not in strict_requested]
                    self._inbox.forget_delivered_notifications(ordinary_sent)
                else:
                    self._inbox.forget_delivered_notifications([value for value in sent if value in terminal])
                    self._inbox.forget_notification_delivery_parts(
                        [value for value in requested if value in terminal]
                    )
                return
            except Exception as exc:  # noqa: PERF203 - the retry IS the point here
                last_error = exc
                if attempt < ack_attempts - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
        # Loud, because it is still a real fault: the backend's queue is not
        # advancing. What it is NOT any more is user-visible duplication.
        LOGGER.error(
            "Outbound ack failed after retries: %d delivered notifications stay pending "
            "on the backend and will be re-acked, not re-sent (%s)",
            len(strict_requested) or len(sent) + len(uncertain),
            type(last_error).__name__,
        )

    async def stop(self) -> None:
        self._running = False
        self._stopping = True
        await self._await_inflight_updates()

    async def _get_updates(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.post(
            f"{self._api_url}/getUpdates",
            json={
                "offset": self._offset,
                "timeout": POLL_TIMEOUT,
                # edited_message принимается, чтобы честно ответить «правки не
                # подхватываю» — иначе чат и база молча расходятся навсегда.
                "allowed_updates": list(ALLOWED_UPDATE_KINDS),
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
        """Запустить работу по готовым обновлениям и вернуться, НЕ дожидаясь её.

        Раньше здесь стоял последовательный `await` на каждую строку, а
        `_poll_loop` зовёт этот обход ПЕРЕД `_get_updates`. Значит один долгий ход
        держал и остальную пачку, и сам опрос Telegram: после 0.171.0 мост честно
        ждёт ответа ядра до 780 с, и всё это время у остальных были мертвы и чат,
        и кнопки — они приходят теми же обновлениями.

        Порядок внутри чата защищает хранилище, а не эта функция: `pending()`
        отдаёт ровно ОДНУ готовую строку на `ordering_key`. Поэтому строки одной
        пачки принадлежат разным чатам по построению, и запускать их одновременно
        безопасно. Единственное, что нужно добавить, — не брать строку чата, у
        которого работа уже в полёте.
        """

        self._dispatch_ready_updates(telegram, backend)

    def _dispatch_ready_updates(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
    ) -> int:
        """Раздать готовые строки задачам. Синхронно — здесь нечего ждать.

        Синхронность существенна: эту же раздачу зовёт задача, которая только что
        закончилась, чтобы следующая строка её чата ушла в работу немедленно, а не
        ждала следующего опроса Telegram (а он длинный). Без этого продолжения
        разбор накопившейся очереди замедлился бы ровно во столько раз, во сколько
        длинный опрос дольше хода.
        """

        if self._stopping:
            return 0
        started = 0
        blocked_keys = set(self._inflight)
        while len(self._inflight) < MAX_CONCURRENT_UPDATES and started < BATCH_SIZE:
            room = MAX_CONCURRENT_UPDATES - len(self._inflight)
            rows = [
                row
                for row in self._inbox.pending(limit=room + len(blocked_keys))
                if str(row["ordering_key"]) not in blocked_keys
            ][:room]
            if not rows:
                break
            for row in rows:
                ordering_key = str(row["ordering_key"])
                blocked_keys.add(ordering_key)
                task = asyncio.create_task(self._run_update(telegram, backend, dict(row)))
                self._inflight[ordering_key] = task
                started += 1
        return started

    async def _run_update(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        row: dict[str, Any],
    ) -> None:
        """Один ход: обработать обновление и учесть исход РОВНО ОДИН раз."""

        update_id = int(row["update_id"])
        ordering_key = str(row["ordering_key"])
        update: dict[str, Any] = {}
        owned_update_ids = [update_id]
        cancelled = False
        try:
            update = json.loads(row["payload_json"])
            update, album_rows = await self._collect_media_group(row, update)
            owned_update_ids = [int(item["update_id"]) for item in album_rows]
            cached = json.loads(row["backend_response_json"]) if row.get("backend_response_json") else None
            await self._process_update(telegram, backend, update, cached_response=cached)
            self._inbox.remove_many(owned_update_ids)
        except PermanentUpdateError as exc:
            owned_update_ids = list(dict.fromkeys(getattr(exc, "update_ids", owned_update_ids)))
            LOGGER.warning("Quarantining invalid Telegram update (%s)", type(exc).__name__)
            self._inbox.mark_dead_letter_many(owned_update_ids, type(exc).__name__)
            # MediaTooLargeError already told the user; others left them in
            # silence — a rejected message must never just vanish.
            if not isinstance(exc, MediaTooLargeError):
                await self._notify_dead_letter(telegram, update, permanent=True)
        except asyncio.CancelledError:
            # Отмена — не отказ обновления: строка остаётся ожидающей, попытка не
            # тратится. Иначе остановка моста съедала бы людям попытки.
            cancelled = True
            raise
        except Exception as exc:
            LOGGER.warning("Telegram update deferred (%s)", type(exc).__name__)
            dead_lettered = self._inbox.mark_failure_many(owned_update_ids, type(exc).__name__)
            if dead_lettered:
                self._inbox.mark_dead_letter_many(owned_update_ids, type(exc).__name__)
                LOGGER.error("Telegram update exhausted its retry budget")
                await self._notify_dead_letter(telegram, update, permanent=False)
        finally:
            for owned_update_id in owned_update_ids:
                self._archive_passwords.pop(owned_update_id, None)
            # Чат освобождается ЗДЕСЬ, а не в чужом обходе: только эта задача
            # знает, что её работа кончилась, и только отсюда следующая строка
            # того же чата может уйти в работу немедленно.
            self._inflight.pop(ordering_key, None)
            # После ОТМЕНЫ продолжения нет: отменяют при разборке, и очередь к
            # этому моменту может быть уже закрыта — раздача полезла бы в мёртвую
            # базу вместо того, чтобы дать процессу спокойно завершиться.
            if not cancelled:
                self._dispatch_ready_updates(telegram, backend)

    @staticmethod
    def _media_group_id(update: dict[str, Any]) -> str:
        message = update.get("message")
        if not isinstance(message, dict):
            return ""
        group_id = str(message.get("media_group_id") or "")
        if not group_id:
            return ""
        if len(group_id) > 128 or not group_id.isascii():
            raise PermanentUpdateError("Telegram media group id is invalid")
        return group_id

    async def _collect_media_group(
        self,
        anchor_row: dict[str, Any],
        anchor_update: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Turn one Telegram album into one durable backend turn.

        Telegram provides a shared group id but no final-item marker, and a
        group may straddle two long-poll responses.  Keep the chat's FIFO slot
        while waiting for a short quiet period; polling remains live and stores
        later parts.  Only a contiguous, same-chat/same-sender group is owned.
        """

        group_id = self._media_group_id(anchor_update)
        if not group_id:
            return anchor_update, [anchor_row]
        deadline = time.monotonic() + _ALBUM_MAX_WAIT_SEC
        rows: list[dict[str, Any]] = [anchor_row]
        while True:
            candidates = self._inbox.contiguous_pending_rows(
                str(anchor_row["ordering_key"]),
                int(anchor_row["update_id"]),
                limit=BATCH_SIZE * 2,
            )
            grouped: list[dict[str, Any]] = []
            for candidate in candidates:
                try:
                    candidate_update = json.loads(candidate["payload_json"])
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise PermanentUpdateError("Telegram album row is invalid") from exc
                if self._media_group_id(candidate_update) != group_id:
                    break
                grouped.append(candidate)
            rows = grouped or [anchor_row]
            if len(rows) > _ALBUM_MAX_ITEMS:
                raise _AlbumPermanentError("Telegram media group is too large", rows)
            newest_created_at = max(float(item.get("created_at") or 0.0) for item in rows)
            quiet_remaining = _ALBUM_SETTLE_SEC - max(0.0, time.time() - newest_created_at)
            remaining = deadline - time.monotonic()
            if quiet_remaining <= 0 or remaining <= 0:
                break
            await asyncio.sleep(min(quiet_remaining, remaining))

        messages: list[dict[str, Any]] = []
        captions: list[str] = []
        chat_user_identity: tuple[str, str] | None = None
        message_ids: set[int] = set()
        for candidate in rows:
            candidate_update = json.loads(candidate["payload_json"])
            if candidate.get("backend_response_json") and int(candidate["update_id"]) != int(
                anchor_row["update_id"]
            ):
                raise _AlbumPermanentError("Telegram album was partially processed", rows)
            message = candidate_update.get("message")
            if not isinstance(message, dict):
                raise _AlbumPermanentError("Telegram album part has no message", rows)
            message_id = message.get("message_id")
            if (
                not isinstance(message_id, int)
                or isinstance(message_id, bool)
                or not 0 < message_id <= (2**63 - 1)
                or message_id in message_ids
            ):
                raise _AlbumPermanentError("Telegram album message identity is invalid", rows)
            message_ids.add(message_id)
            chat = message.get("chat")
            sender = message.get("from")
            identity = (
                str(chat.get("id") or "") if isinstance(chat, dict) else "",
                str(sender.get("id") or "") if isinstance(sender, dict) else "",
            )
            if not all(identity) or (chat_user_identity is not None and identity != chat_user_identity):
                raise _AlbumPermanentError("Telegram album identity changed", rows)
            chat_user_identity = identity
            messages.append(dict(message))
            caption = str(message.get("caption") or "").strip()
            if caption and caption not in captions:
                captions.append(caption)
        if len(captions) > 1:
            raise _AlbumPermanentError("Telegram album has conflicting captions", rows)
        combined = copy.deepcopy(anchor_update)
        combined_message = dict(messages[0])
        if captions:
            combined_message["caption"] = captions[0]
        combined["message"] = combined_message
        combined["friday_media_group_messages"] = messages
        return combined, rows

    async def _await_inflight_updates(self) -> None:
        """Дождаться работы в полёте. Брошенная задача — обновление, снятое с
        очереди и не отвеченное, поэтому остановка ждёт, а не рубит."""

        while self._inflight:
            tasks = list(self._inflight.values())
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _update_chat_id(update: dict[str, Any]) -> int | None:
        """Чат обновления — ЛЮБОГО из трёх видов, которые мы запрашиваем.

        Читалось только `update["message"]["chat"]`, а мост подписан на три вида
        (`allowed_updates` в опросе): `message`, `edited_message`, `callback_query`.
        У нажатой кнопки чат лежит на этаж глубже — в `callback_query.message`, —
        и функция возвращала `None`. Воспроизведено: тот же чат находится у текста
        и не находится у кнопки.

        Единственный потребитель — уведомление о неудаче, поэтому цена ровно такая:
        человек нажал «Подтвердить», попытки исчерпались, и он не узнал НИЧЕГО.
        Молчание после нажатия читается как «сделано», и это худшее из возможных
        толкований: пропавший вопрос человек хотя бы задаст ещё раз, а пропавшее
        подтверждение он считает исполненным.

        Виды перечислены здесь все, а не только тот, на котором нашлась поломка:
        мост подписывается на список видов в одном месте, а разбирал их в другом,
        и расхождение этих двух мест — и есть класс ошибки. Новый вид в подписке
        снова окажется невидимым, но ниже стоит тест, который сверяет оба списка.
        """
        if not isinstance(update, dict):
            return None
        carriers = [update.get("message"), update.get("edited_message")]
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            carriers.append(callback.get("message"))
        for carrier in carriers:
            chat = carrier.get("chat") if isinstance(carrier, dict) else None
            if not isinstance(chat, dict):
                continue
            try:
                found = int(chat.get("id", 0)) or None
            except (TypeError, ValueError):
                continue
            if found is not None:
                return found
        return None

    def _may_message_chat(self, chat_id: int) -> bool:
        """Кому боту вообще позволено писать: статический список ИЛИ чат, который
        уже впустила открытая регистрация.

        Один предикат на все точки — исходящий цикл, обработчик кнопок и
        уведомление о неудаче. Пока их было три копии, одна отстала: newcomer
        получал кнопки и рассылки, но на неудачу ему не отвечал никто.
        """
        return chat_id in self.config.allowed_chat_ids or self._inbox.is_registered_chat(chat_id)

    async def _notify_dead_letter(
        self, telegram: httpx.AsyncClient, update: dict[str, Any], *, permanent: bool
    ) -> None:
        """Tell the originating chat its message could not be processed, so a
        dead-lettered update is never pure silence. Deny-by-default, with the
        SAME predicate the outbound loop and the callback handler already use:
        an allowlisted chat, or one open registration already admitted.

        Without the second half, a self-registered newcomer — the very account
        open registration exists to create — got no notice at all: their message
        vanished and the bot looked broken to the only person who cannot know
        why. Best-effort delivery."""
        chat_id = self._update_chat_id(update)
        if chat_id is None or not self._may_message_chat(chat_id):
            return
        # Нажатие кнопки — не «сообщение», и говорить о нём как о сообщении значит
        # оставить человека в неведении о судьбе именно РЕШЕНИЯ. «Кнопка не
        # сработала, решение не принято» отвечает на тот вопрос, который у него
        # есть: подтвердилось ли действие.
        pressed = isinstance(update, dict) and isinstance(update.get("callback_query"), dict)
        if pressed:
            text = (
                "⚠️ Кнопка не сработала — решение НЕ принято. Откройте заявку и нажмите ещё раз."
                if permanent
                else "⚠️ Кнопка не сработала, решение пока НЕ принято. Я повторю попытку; "
                "если ответа не будет, нажмите ещё раз."
            )
        else:
            text = (
                "⚠️ Не удалось обработать это сообщение — оно отклонено."
                if permanent
                else "⚠️ Не удалось обработать это сообщение, я отложил его. "
                "Попробуйте позже или переформулируйте."
            )
        try:
            await self._send_message(telegram, chat_id, text)
        except Exception as exc:
            LOGGER.warning("dead-letter notice failed (%s)", type(exc).__name__)

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
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": external_user_id,
            "X-Friday-Chat": chat_id,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": signature,
        }
        response = await client.request(
            method,
            f"{self._backend_url}{path}",
            content=body if body else None,
            headers=headers,
        )
        if response.status_code == 409 and not response.headers.get("Retry-After", "").strip():
            detail = response.text[:500]
            raise PermanentUpdateError(f"Backend rejected update (409): {detail}", status_code=409)
        if response.status_code in {400, 403, 404, 413, 422}:
            detail = response.text[:500]
            raise PermanentUpdateError(
                f"Backend rejected update ({response.status_code}): {detail}",
                status_code=response.status_code,
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Backend returned a non-object response")
        return data

    async def _backend_text(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        external_user_id: str,
        chat_id: str,
    ) -> str:
        """Same signed bridge call as ``_backend_json``, but return raw text body.

        Used by ``/export``: the backend answers ``text/plain``, not JSON.
        """
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
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": external_user_id,
            "X-Friday-Chat": chat_id,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": signature,
        }
        response = await client.request(
            method,
            f"{self._backend_url}{path}",
            content=body if body else None,
            headers=headers,
        )
        if response.status_code == 409 and not response.headers.get("Retry-After", "").strip():
            detail = response.text[:500]
            raise PermanentUpdateError(f"Backend rejected update (409): {detail}", status_code=409)
        if response.status_code in {400, 403, 404, 413, 422}:
            detail = response.text[:500]
            raise PermanentUpdateError(
                f"Backend rejected update ({response.status_code}): {detail}",
                status_code=response.status_code,
            )
        response.raise_for_status()
        return response.text

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
        except Exception as exc:
            LOGGER.debug("Telegram typing indicator failed (%s)", type(exc).__name__)

    async def _send_message(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        resume_key: int | None = None,
        text_format: str = "markdown",
        reply_source_message_id: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        """Отправить текст, при необходимости несколькими кусками.

        `resume_key` — номер обновления, ответ на которое отправляется. С ним
        отправка становится ПРОДОЛЖАЕМОЙ: каждый ушедший кусок отмечается в
        durable-очереди, и повтор после обрыва начинает с места обрыва, а не с
        начала. Без него (служебные сообщения, подсказки, уведомления) поведение
        прежнее — такие сообщения короткие и повторяются целиком.

        Продолжение корректно только потому, что ответ ядра КЕШИРУЕТСЯ до первой
        отправки: повтор режет тот же самый текст и получает те же самые границы.
        Если бы текст мог измениться между попытками, номер куска ничего не значил
        бы.
        """
        # Режем СЫРОЙ текст, размечаем каждый кусок отдельно. Наоборот было бы
        # опаснее: граница в 4096 знаков попала бы внутрь тега, и Telegram
        # отверг бы кусок целиком. При разрыве абзаца пополам жирное начертание
        # в этом месте теряется — это видно, но сообщение доходит.
        chunks = split_for_telegram(text)
        already_sent = 0
        if resume_key is not None:
            already_sent = min(self._inbox.answer_chunks_sent(resume_key), len(chunks))
        reply_parameters: dict[str, Any] | None = None
        if (
            isinstance(reply_to_message_id, int)
            and not isinstance(reply_to_message_id, bool)
            and 0 < reply_to_message_id <= (2**63 - 1)
        ):
            reply_parameters = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if (
            resume_key is not None
            and self._inbox.answer_delivery_uncertainty_pending(resume_key)
            and self._inbox.begin_answer_delivery_uncertainty_notice(resume_key)
        ):
            notice_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": _DELIVERY_UNCERTAINTY_NOTICE,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_parameters is not None:
                notice_payload["reply_parameters"] = reply_parameters
            try:
                notice_response = await self._post_message_chunk(
                    client,
                    notice_payload,
                    _DELIVERY_UNCERTAINTY_NOTICE,
                )
                notice_response.raise_for_status()
            except httpx.ConnectError:
                # No response and no accepted-write evidence: this narrow
                # pre-accept failure remains retryable.
                self._inbox.retry_answer_delivery_uncertainty_notice(resume_key)
                raise
            except httpx.HTTPStatusError:
                # A concrete HTTP response is proof of rejection. Ambiguous
                # transport errors (read/write/protocol/pool) deliberately do
                # not enter this branch: the warning may already have arrived.
                self._inbox.retry_answer_delivery_uncertainty_notice(resume_key)
                raise
        plain_text = text_format == "plain"
        for index, chunk in enumerate(chunks):
            if index < already_sent:
                # Этот кусок человек уже получил на прошлой попытке.
                continue
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk if plain_text else (to_telegram_html(chunk) or chunk),
                "disable_web_page_preview": True,
            }
            if not plain_text:
                payload["parse_mode"] = "HTML"
            if reply_parameters is not None:
                payload["reply_parameters"] = reply_parameters
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            if resume_key is None:
                # Service/administrative messages do not participate in the
                # durable answer cursor.  Keep that non-resumable seam narrow:
                # no synthetic ``None`` delivery identity is passed to custom
                # transports or test adapters.
                response = await self._post_message_chunk(client, payload, chunk)
            else:
                response = await self._post_message_chunk(
                    client,
                    payload,
                    chunk,
                    resume_key=resume_key,
                    chunk_number=index + 1,
                )
            response.raise_for_status()
            if reply_source_message_id:
                body = response.json()
                result = body.get("result") if isinstance(body, dict) else None
                try:
                    telegram_message_id = (
                        int(result.get("message_id") or 0) if isinstance(result, dict) else 0
                    )
                except (TypeError, ValueError):
                    telegram_message_id = 0
                if telegram_message_id > 0:
                    # Commit opaque lineage while the pre-write uncertainty
                    # fence is still armed. A crash here skips the possibly
                    # accepted chunk on restart instead of duplicating it.
                    self._inbox.remember_outbound_reply_context(
                        chat_id,
                        telegram_message_id,
                        reply_source_message_id,
                    )
            # The pre-write fence remains deliberately uncertain until the
            # accepted response and its opaque reply lineage have both been
            # handled.  A hard kill anywhere above therefore skips this chunk
            # on restart and emits one bounded notice, never a second copy of
            # possibly accepted model text.
            if resume_key is not None and not self._inbox.confirm_answer_chunk_delivery(
                resume_key,
                index + 1,
            ):
                raise RuntimeError("Telegram answer delivery fence could not be confirmed")

    async def _send_message_returning_id(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        text: str,
    ) -> int:
        """Короткое сообщение, чей `message_id` нужен вызывающему.

        Обычный `_send_message` ничего не возвращает — и правильно: он режет
        длинный ответ на куски, и «идентификатор сообщения» у такого ответа не
        один. Здесь текст заведомо короткий: это приглашение ответить репликой, и
        его идентификатор — то, за что потом цепляется ответ человека.
        """
        payload = {
            "chat_id": chat_id,
            "text": to_telegram_html(text) or text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = await self._post_message_chunk(client, payload, text)
        response.raise_for_status()
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        return int(result.get("message_id") or 0) if isinstance(result, dict) else 0

    #: Сколько раз ждать по просьбе Telegram, прежде чем сдаться.
    _RATE_LIMIT_RETRIES = 3
    #: Потолок ожидания на одну просьбу. Telegram при жёстком лимите просит и
    #: несколько минут; столько держать ход нельзя — лучше честно отдать отказ
    #: наверх, чем занимать слот и молчать.
    _RATE_LIMIT_MAX_WAIT_SEC = 30.0

    async def _post_message_chunk(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        chunk: str,
        *,
        resume_key: int | None = None,
        chunk_number: int | None = None,
    ) -> httpx.Response:
        """Отправить один кусок, пережив разметку и ограничение частоты.

        `429` до этого не читался вовсе: `raise_for_status` ронял ВЕСЬ ход, тот
        уходил в повтор, и уже доставленные куски длинного ответа приходили
        человеку второй раз. Ответ модели при этом не пересчитывался (он лежит в
        кеше обновления) — то есть платил за это только читающий, дубликатами.

        Telegram сам говорит, сколько ждать, в `parameters.retry_after`. Ждём
        столько и повторяем ЭТОТ ЖЕ кусок, поэтому доставка продолжается с места
        остановки, а не начинается заново.
        """

        if (resume_key is None) != (chunk_number is None):
            raise ValueError("resumable Telegram delivery identity is incomplete")

        def begin_delivery() -> tuple[int, int] | None:
            if resume_key is None or chunk_number is None:
                return None
            snapshot = self._inbox.begin_answer_chunk_delivery(resume_key, chunk_number)
            if snapshot is None:
                raise RuntimeError("Telegram answer delivery fence could not be committed")
            return snapshot

        def reject_delivery(snapshot: tuple[int, int] | None) -> None:
            if snapshot is None or resume_key is None or chunk_number is None:
                return
            previous_count, previous_uncertainty = snapshot
            if not self._inbox.reject_answer_chunk_delivery(
                resume_key,
                chunk_number,
                previous_count=previous_count,
                previous_uncertainty=previous_uncertainty,
            ):
                raise RuntimeError("Telegram answer delivery fence could not be rolled back")

        async def post_once() -> tuple[httpx.Response, tuple[int, int] | None]:
            # Commit before constructing the awaitable network seam. If the
            # process dies before, during, or after this POST, restart observes
            # an uncertain/advanced cursor and never resends this chunk.
            snapshot = begin_delivery()
            try:
                response = await client.post(f"{self._api_url}/sendMessage", json=payload)
            except httpx.ConnectError:
                # This exception is raised while establishing the connection;
                # no request reached Telegram, so retrying the exact chunk is
                # safe after restoring the pre-write snapshot.
                reject_delivery(snapshot)
                raise
            return response, snapshot

        for attempt in range(self._RATE_LIMIT_RETRIES):
            response, snapshot = await post_once()
            if response.status_code == 400 and "parse_mode" in payload:
                # Разметка важна, но доставка важнее. Любая неожиданная
                # последовательность — незакрытый тег на границе куска, экзотика
                # из ответа модели — не должна стоить человеку СООБЩЕНИЯ: именно
                # «ничего не приходит» владелец и разбирал сегодня.
                LOGGER.warning("Telegram rejected formatted message; resending as plain text")
                # A concrete HTTP rejection proves non-delivery. Restore the
                # cursor before the schema-less retry gets its own pre-write
                # fence; a kill in between can then safely retry the chunk.
                reject_delivery(snapshot)
                payload.pop("parse_mode", None)
                payload["text"] = chunk
                response, snapshot = await post_once()
            if response.status_code != 429 or attempt == self._RATE_LIMIT_RETRIES - 1:
                if not 200 <= int(response.status_code) < 300:
                    # The response itself is proof Telegram rejected this write.
                    reject_delivery(snapshot)
                # A successful resumable response intentionally keeps the
                # pre-write fence armed. `_send_message` confirms it only after
                # opaque reply-lineage persistence, closing that crash window too.
                return response
            reject_delivery(snapshot)
            wait_sec = self._retry_after_sec(response)
            LOGGER.warning("Telegram rate limit; waiting %.1fs before resending the same chunk", wait_sec)
            await asyncio.sleep(wait_sec)
        return response

    @classmethod
    def _retry_after_sec(cls, response: httpx.Response) -> float:
        """Сколько ждать по просьбе Telegram. Тело важнее заголовка: `retry_after`
        приходит именно в `parameters`, а `Retry-After` бывает не у всех прокси."""

        requested = 0.0
        with suppress(ValueError, TypeError, json.JSONDecodeError):
            body = response.json()
            parameters = body.get("parameters") if isinstance(body, dict) else None
            if isinstance(parameters, dict):
                requested = float(parameters.get("retry_after") or 0.0)
        if requested <= 0:
            with suppress(ValueError, TypeError):
                requested = float(response.headers.get("Retry-After", "") or 0.0)
        # Ноль означал бы busy-loop, поэтому пол — секунда.
        return max(1.0, min(requested, cls._RATE_LIMIT_MAX_WAIT_SEC))

    async def _send_document(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        filename: str,
        content_bytes: bytes,
        *,
        caption: str = "",
        mime_type: str = "text/plain; charset=utf-8",
    ) -> None:
        """Upload a file to the chat (G20 export). Multipart, not JSON sendMessage.

        `mime_type` не косметика: Word, Excel и PDF, отправленные как
        `text/plain`, Telegram показывает текстовым файлом, и на телефоне они
        открываются кракозябрами вместо документа.
        """
        safe_name = (filename or "export.txt").replace("/", "_").replace("\\", "_")[:128]
        response = await client.post(
            f"{self._api_url}/sendDocument",
            data={"chat_id": str(chat_id), "caption": (caption or "")[:1024]},
            files={"document": (safe_name, content_bytes, mime_type)},
        )
        response.raise_for_status()

    async def _send_voice(
        self,
        client: httpx.AsyncClient,
        chat_id: int,
        audio_bytes: bytes,
    ) -> None:
        """Send a synthesized reply as a native Telegram voice bubble.

        `sendVoice` requires an OGG container with the Opus codec for the
        waveform/voice UI (`friday.tts.synthesize_speech` already produces
        exactly that) — unlike `_send_document`, there is no caption or filename;
        the spoken text was already sent as the regular text reply.
        """
        response = await client.post(
            f"{self._api_url}/sendVoice",
            data={"chat_id": str(chat_id)},
            files={"voice": ("reply.ogg", audio_bytes, "audio/ogg")},
        )
        response.raise_for_status()
