"""Durable Telegram long-polling bridge with signed backend requests."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from jericho.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from jericho.security import sign_bridge_request
from jericho.telemetry.logging import install_secret_redaction

LOGGER = logging.getLogger(__name__)
API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 30
BACKOFF_MAX = 60.0
MAX_ATTEMPTS = 288
BATCH_SIZE = 20
TELEGRAM_TEXT_LIMIT = 4096
RETRY_DELAYS_SEC = (2.0, 10.0, 30.0, 60.0, 300.0)
CALLBACK_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    backend_url: str = "http://127.0.0.1:8000"
    bridge_secret: str = ""
    allowed_chat_ids: list[int] = field(default_factory=list)
    inbox_db_path: str = "telegram_inbox.sqlite3"
    max_document_bytes: int = 50 * 1024 * 1024
    backend_timeout_sec: float = 300.0
    outbound_poll_interval_sec: float = 15.0

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise ValueError("A valid Telegram bot token is required")
        if len(self.bridge_secret) < 32:
            raise ValueError("Telegram bridge secret must contain at least 32 characters")
        if not self.backend_url.startswith(("http://", "https://")):
            raise ValueError("backend_url must use HTTP or HTTPS")
        if not self.allowed_chat_ids:
            # Deny-by-default: refuse to run an open bot. The effective allowlist
            # (allowlist plus owner chats) is supplied by the caller.
            raise ValueError(
                "No allowed Telegram chats configured; set "
                "JERICHO_TELEGRAM_ALLOWED_CHAT_IDS or JERICHO_TELEGRAM_OWNER_CHAT_IDS"
            )


class _UpdateInbox:
    """SQLite queue: Telegram offsets advance only after an update is durable."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS updates (
                update_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                backend_response_json TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                next_attempt_at REAL NOT NULL DEFAULT 0,
                failed_at REAL
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Idempotent upgrade from the original durable-inbox schema.  Add
        # columns before recreating the index so an existing database can never
        # fail halfway through startup because an index references new fields.
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(updates)").fetchall()}
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "next_attempt_at": "REAL NOT NULL DEFAULT 0",
            "failed_at": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE updates ADD COLUMN {name} {definition}")
        self._conn.execute(
            """UPDATE updates
               SET status='dead_letter', failed_at=COALESCE(failed_at, last_attempt_at)
               WHERE attempts>=? AND status='pending'""",
            (MAX_ATTEMPTS,),
        )
        self._conn.execute("DROP INDEX IF EXISTS idx_updates_pending")
        self._conn.execute(
            """CREATE INDEX idx_updates_pending
               ON updates(status, next_attempt_at, update_id)"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_offset(self) -> int:
        row = self._conn.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row["value"]) if row else 0

    def set_offset(self, offset: int) -> None:
        self._conn.execute(
            """INSERT INTO state(key, value) VALUES('offset', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(max(0, int(offset))),),
        )
        self._conn.commit()

    def store(self, update: dict[str, Any]) -> bool:
        update_id = int(update.get("update_id", -1))
        if update_id < 0:
            return False
        cursor = self._conn.execute(
            """INSERT OR IGNORE INTO updates(update_id, payload_json, created_at)
               VALUES(?, ?, ?)""",
            (update_id, json.dumps(update, ensure_ascii=False, sort_keys=True), time.time()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        ready_at = time.time() if now is None else float(now)
        rows = self._conn.execute(
            """SELECT * FROM updates
               WHERE status='pending' AND attempts < ? AND next_attempt_at <= ?
               ORDER BY update_id ASC LIMIT ?""",
            (MAX_ATTEMPTS, ready_at, BATCH_SIZE),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_failure(self, update_id: int, error: str) -> bool:
        row = self._conn.execute(
            "SELECT attempts FROM updates WHERE update_id=?",
            (update_id,),
        ).fetchone()
        if row is None:
            return False
        attempts = int(row["attempts"]) + 1
        attempted_at = time.time()
        dead_lettered = attempts >= MAX_ATTEMPTS
        delay = RETRY_DELAYS_SEC[min(attempts - 1, len(RETRY_DELAYS_SEC) - 1)]
        self._conn.execute(
            """UPDATE updates
               SET attempts=?, last_attempt_at=?, last_error=?, status=?,
                   next_attempt_at=?, failed_at=?
               WHERE update_id=?""",
            (
                attempts,
                attempted_at,
                error[:500],
                "dead_letter" if dead_lettered else "pending",
                0 if dead_lettered else attempted_at + delay,
                attempted_at if dead_lettered else None,
                update_id,
            ),
        )
        self._conn.commit()
        return dead_lettered

    def mark_dead_letter(self, update_id: int, error: str) -> None:
        failed_at = time.time()
        self._conn.execute(
            """UPDATE updates
               SET status='dead_letter', last_error=?, last_attempt_at=?,
                   next_attempt_at=0, failed_at=?
               WHERE update_id=?""",
            (error[:500], failed_at, failed_at, update_id),
        )
        self._conn.commit()

    def dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM updates WHERE status='dead_letter'
               ORDER BY failed_at DESC, update_id ASC LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS count FROM updates GROUP BY status").fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "pending": counts.get("pending", 0),
            "dead_letter": counts.get("dead_letter", 0),
        }

    def cache_backend_response(self, update_id: int, response: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE updates SET backend_response_json=? WHERE update_id=?",
            (json.dumps(response, ensure_ascii=False, sort_keys=True), update_id),
        )
        self._conn.commit()

    def remove(self, update_id: int) -> None:
        self._conn.execute("DELETE FROM updates WHERE update_id=?", (update_id,))
        self._conn.commit()


class PermanentUpdateError(RuntimeError):
    """The update cannot become valid after a retry."""


class MediaTooLargeError(PermanentUpdateError):
    """A media file exceeded the configured size limit; the user is told, not dead-lettered silently."""


# Single-descriptor media fields on a Telegram message (photo is handled
# separately because it is a list of sizes). Each entry drives the download and
# the media_kind/mime/filename the file is ingested with.
_SINGLE_MEDIA_FIELDS: tuple[tuple[str, str, str, str, bool], ...] = (
    # (message field, media_kind, default mime, filename suffix, use file_name)
    ("document", "document", "application/octet-stream", "bin", True),
    ("voice", "voice", "audio/ogg", "ogg", False),
    ("audio", "audio", "audio/mpeg", "mp3", True),
    ("video", "video", "video/mp4", "mp4", True),
    ("video_note", "video_note", "video/mp4", "mp4", False),
    ("animation", "animation", "video/mp4", "mp4", True),
)


class TelegramBridge:
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
        install_secret_redaction((self.config.bot_token, self.config.bridge_secret))
        try:
            self._lease.acquire()
        except RuntimeLeaseError:
            self._inbox.close()
            raise
        self._running = True
        timeout = httpx.Timeout(POLL_TIMEOUT + 10.0, connect=15.0)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout, trust_env=False) as telegram,
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self.config.backend_timeout_sec, connect=15.0),
                    trust_env=False,
                ) as backend,
            ):
                LOGGER.info("Telegram bridge started at offset %d", self._offset)
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
                break

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
        command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold() if text.startswith("/") else ""

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
            query = text.partition(" ")[2].strip()
            await self._send_browse(telegram, backend, chat_id, external_user_id, user, query)
            return
        if command == "/search":
            query = text.partition(" ")[2].strip()
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
            goal = text.partition(" ")[2].strip()
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
            text = text.partition(" ")[2].strip()
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

    async def _send_inbox(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/inbox?status=pending&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(telegram, chat_id, "Inbox пуст — сейчас подтверждать нечего.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Ближайшие предложения Inbox: {len(items)}. Продвижение создаёт долгосрочное знание; "
            "игнорирование сохраняет историю решения.",
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            inbox_id = str(item.get("id") or "")
            if not inbox_id:
                continue
            suggestions = item.get("suggestions")
            if not isinstance(suggestions, dict):
                try:
                    suggestions = json.loads(str(item.get("suggestions_json") or "{}"))
                except json.JSONDecodeError:
                    suggestions = {}
            title = str(suggestions.get("title") or "Материал на review")[:160]
            summary = str(
                suggestions.get("summary")
                or item.get("classification_notes")
                or "Откройте Admin UI для детальной коррекции структуры."
            )[:700]
            kind = str(suggestions.get("knowledge_kind") or "note")
            await self._send_message(
                telegram,
                chat_id,
                f"{title}\n\n{summary}\n\nТип: {kind}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "✓ В знания", "callback_data": f"inbox:promote:{inbox_id}"},
                            {"text": "✕ Игнорировать", "callback_data": f"inbox:ignore:{inbox_id}"},
                        ]
                    ]
                },
            )

    _MERGE_RECOMMENDATION_LABELS = {
        "strong_merge_candidate": "вероятный дубликат",
        "compare_context": "похоже, сверьте контекст",
        "manual_review": "нужна ручная проверка",
    }

    async def _send_merges(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/kg/resolutions/pending",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(telegram, chat_id, "Кандидатов на объединение сущностей нет.")
            return
        await self._send_message(
            telegram,
            chat_id,
            f"Возможные дубликаты сущностей: {len(items)}. Объединение переносит связи и "
            "историю на одну сущность; «не дубликат» сохраняет обе и не предложит пару снова.",
        )
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "")
            entity_a = item.get("entity_a") if isinstance(item.get("entity_a"), dict) else {}
            entity_b = item.get("entity_b") if isinstance(item.get("entity_b"), dict) else {}
            if not candidate_id or not entity_a or not entity_b:
                continue
            recommendation = self._MERGE_RECOMMENDATION_LABELS.get(
                str(item.get("recommendation") or ""), "предложение"
            )
            confidence = round(float(item.get("confidence") or 0.0) * 100)
            left = self._describe_merge_entity(entity_a)
            right = self._describe_merge_entity(entity_b)
            await self._send_message(
                telegram,
                chat_id,
                f"Объединить сущности?\n\n• {left}\n• {right}\n\nОценка: {recommendation} ({confidence}%).",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🔗 Объединить", "callback_data": f"merge:accept:{candidate_id}"},
                            {"text": "✕ Не дубликат", "callback_data": f"merge:reject:{candidate_id}"},
                        ]
                    ]
                },
            )

    @staticmethod
    def _describe_merge_entity(entity: dict[str, Any]) -> str:
        name = str(entity.get("name") or "без имени")[:120]
        entity_type = str(entity.get("entity_type") or "").strip()
        knowledge = int(entity.get("knowledge_count") or 0)
        relations = int(entity.get("relation_count") or 0)
        suffix = f" — {entity_type}" if entity_type else ""
        return f"{name}{suffix} (знаний: {knowledge}, связей: {relations})"

    async def _send_tags(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/knowledge/tags?limit=25",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(
                telegram,
                chat_id,
                "Тегов пока нет. Они появляются при разборе материалов в Inbox и при сохранении заметок.",
            )
            return
        lines = ["Теги вашей базы знаний:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").strip()
            if tag:
                lines.append(f"• #{tag} — {int(item.get('count') or 0)}")
        lines.append("\nПоказать записи: /browse тег")
        await self._send_message(telegram, chat_id, "\n".join(lines))

    _CONTAINER_KIND_LABELS = {"project": "проект", "collection": "коллекция"}

    async def _send_browse(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        query: str,
    ) -> None:
        if not query:
            # No argument: show the container tree as browse entry points.
            data = await self._backend_json(
                backend,
                "GET",
                "/api/kg/containers",
                {"telegram_user": telegram_user},
                external_user_id,
                str(chat_id),
            )
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                await self._send_message(
                    telegram,
                    chat_id,
                    "Использование: /browse тег, проект или сущность\n\n"
                    "Контейнеров (проектов/коллекций) пока нет. Список тегов: /tags",
                )
                return
            by_parent: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                if isinstance(item, dict):
                    by_parent.setdefault(str(item.get("parent_id") or ""), []).append(item)

            lines = ["Ваши проекты и коллекции:"]

            def add_level(parent_key: str, indent: str) -> None:
                for container in by_parent.get(parent_key, []):
                    kind = self._CONTAINER_KIND_LABELS.get(
                        str(container.get("entity_type") or ""), "контейнер"
                    )
                    name = str(container.get("name") or "без имени")[:100]
                    count = int(container.get("knowledge_count") or 0)
                    lines.append(f"{indent}• {name} — {kind}, знаний: {count}")
                    add_level(str(container.get("id") or ""), indent + "   ")

            add_level("", "")
            lines.append("\nПоказать записи: /browse название")
            await self._send_message(telegram, chat_id, "\n".join(lines))
            return

        clean = query.lstrip("#").strip()
        if not clean:
            await self._send_message(telegram, chat_id, "Использование: /browse тег или название")
            return
        # A tag match wins; otherwise fall back to entity/container name search.
        data = await self._backend_json(
            backend,
            "GET",
            f"/api/knowledge?tag={quote(clean, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if items:
            await self._send_message(
                telegram,
                chat_id,
                self._format_browse_results(f"Записи с тегом #{clean}", items),
            )
            return
        found = await self._backend_json(
            backend,
            "GET",
            f"/api/kg/entities?q={quote(clean, safe='')}&limit=5",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        entities = found.get("items") if isinstance(found.get("items"), list) else []
        entity = entities[0] if entities and isinstance(entities[0], dict) else None
        if not entity:
            await self._send_message(
                telegram,
                chat_id,
                f"По запросу «{clean}» не нашлось ни тега, ни сущности. Список тегов: /tags",
            )
            return
        entity_id = str(entity.get("id") or "")
        linked = await self._backend_json(
            backend,
            "GET",
            f"/api/knowledge?entity_id={quote(entity_id, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        knowledge = linked.get("items") if isinstance(linked.get("items"), list) else []
        name = str(entity.get("name") or clean)
        if not knowledge:
            await self._send_message(
                telegram,
                chat_id,
                f"«{name}» найдена, но подтверждённых записей у неё пока нет.",
            )
            return
        await self._send_message(
            telegram,
            chat_id,
            self._format_browse_results(f"Записи «{name}»", knowledge),
        )

    @staticmethod
    def _format_browse_results(header: str, items: list[Any]) -> str:
        lines = [f"{header}:"]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Без названия")[:120]
            kind = str(item.get("knowledge_kind") or "note")
            stage = str(item.get("lifecycle_stage") or "active")
            marker = "" if stage == "active" else f", {stage}"
            lines.append(f"• {title} ({kind}{marker})")
        return "\n".join(lines)

    async def _send_search(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
        query: str,
    ) -> None:
        # Deterministic full-text/hybrid retrieval over confirmed knowledge — no
        # LLM. Reuses GET /api/search (HybridSearcher, scoped to the acting user).
        if not query:
            await self._send_message(
                telegram,
                chat_id,
                "Использование: /search запрос — прямой поиск по базе, без ответа модели",
            )
            return
        data = await self._backend_json(
            backend,
            "GET",
            f"/api/search?q={quote(query, safe='')}&limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if not results:
            await self._send_message(
                telegram,
                chat_id,
                f"По запросу «{query}» ничего не нашлось. Поиск идёт по подтверждённым "
                "записям — возможно, материал ещё ждёт разбора в Inbox (/inbox).",
            )
            return
        await self._send_message(telegram, chat_id, self._format_search_results(query, results))

    @staticmethod
    def _format_search_results(query: str, results: list[Any]) -> str:
        lines = [f"Найдено по запросу «{query}»:"]
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "Без названия")[:120]
            kind = str(item.get("knowledge_kind") or "note")
            stage = str(item.get("lifecycle_stage") or "active")
            marker = "" if stage == "active" else f", {stage}"
            lines.append(f"• {title} ({kind}{marker})")
            snippet = str(item.get("summary") or item.get("content") or "").strip().replace("\n", " ")
            if snippet:
                lines.append(f"  {snippet[:160]}")
        return "\n".join(lines)

    _MISSION_STATUS_LABELS = {
        "proposed": "ожидает запуска",
        "ready": "готова к выполнению",
        "running": "выполняется",
        "paused": "на паузе",
        "blocked": "заблокирована (автономия выключена)",
        "completed": "завершена",
        "failed": "ошибка",
        "cancelled": "отменена",
    }
    _MISSION_ACTIVE_STATUSES = frozenset({"proposed", "ready", "running", "paused", "blocked"})

    def _format_mission_created(self, mission: dict[str, Any]) -> str:
        title = str(mission.get("title") or "Миссия")
        status = str(mission.get("status") or "")
        label = self._MISSION_STATUS_LABELS.get(status, status or "создана")
        tasks = mission.get("task_count") or len(mission.get("tasks") or [])
        lines = [f"Миссия принята: {title}", f"Шагов в плане: {tasks}", f"Статус: {label}."]
        if status == "proposed":
            lines.append("Отправьте /missions, чтобы запустить её кнопкой.")
        elif status == "blocked":
            lines.append("Автономия выключена — выполнение не начнётся, пока её не включат.")
        else:
            lines.append("Итоги шагов придут в Inbox на подтверждение.")
        return "\n".join(lines)

    async def _send_missions(
        self,
        telegram: httpx.AsyncClient,
        backend: httpx.AsyncClient,
        chat_id: int,
        external_user_id: str,
        telegram_user: dict[str, Any],
    ) -> None:
        data = await self._backend_json(
            backend,
            "GET",
            "/api/missions?limit=8",
            {"telegram_user": telegram_user},
            external_user_id,
            str(chat_id),
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if not items:
            await self._send_message(
                telegram,
                chat_id,
                "Миссий пока нет. Создайте: /mission цель миссии",
            )
            return
        await self._send_message(telegram, chat_id, f"Ваши миссии: {len(items)}.")
        for item in items:
            if not isinstance(item, dict):
                continue
            mission_id = str(item.get("id") or "")
            if not mission_id:
                continue
            title = str(item.get("title") or item.get("goal") or "Миссия")[:200]
            status = str(item.get("status") or "")
            label = self._MISSION_STATUS_LABELS.get(status, status)
            done = item.get("done_count") or 0
            total = item.get("task_count") or 0
            text = f"{title}\n\nСтатус: {label}. Шаги: {done}/{total}."
            buttons: list[dict[str, Any]] = []
            if status == "proposed":
                buttons.append({"text": "▶ Запустить", "callback_data": f"mission:start:{mission_id}"})
            if status in self._MISSION_ACTIVE_STATUSES:
                buttons.append({"text": "✕ Остановить", "callback_data": f"mission:stop:{mission_id}"})
            reply_markup = {"inline_keyboard": [buttons]} if buttons else None
            await self._send_message(telegram, chat_id, text, reply_markup=reply_markup)

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
        # Deny-by-default: chats outside the allowlist cannot trigger actions.
        if chat_id not in self.config.allowed_chat_ids:
            await self._answer_callback(telegram, callback_id, "Действие недоступно", alert=True)
            return

        parts = data.split(":", 2)
        clear_markup = False
        try:
            if len(parts) != 3:
                raise PermanentUpdateError("Unknown callback action")
            family, action, target_id = parts
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
                await self._clear_inline_markup(telegram, chat_id, telegram_message_id)

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
        caution = str(response.get("verification_caution") or "").strip()
        if caution:
            body = f"{body}\n\n{caution}"
        legend = str(response.get("citation_notice") or "").strip()
        if legend:
            body = f"{body}\n\n{legend}"
        return body

    async def _answer_callback(
        self,
        client: httpx.AsyncClient,
        callback_id: str,
        text: str,
        *,
        alert: bool = False,
    ) -> None:
        response = await client.post(
            f"{self._api_url}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
        )
        response.raise_for_status()

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

    # Message content fields we knowingly do not ingest; anything else (service
    # messages, empty payloads) is acknowledged silently without a reply.
    _UNSUPPORTED_LABELS = (
        ("sticker", "стикер"),
        ("poll", "опрос"),
        ("dice", "кубик"),
        ("game", "игру"),
        ("story", "историю"),
    )

    @classmethod
    def _unsupported_label(cls, message: dict[str, Any]) -> str | None:
        for content_field, label in cls._UNSUPPORTED_LABELS:
            if message.get(content_field) is not None:
                return label
        return None

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
