"""FastAPI application for Friday's local-first knowledge runtime."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from friday import __version__
from friday.admin_api import router as admin_router
from friday.agent_runtime import AgentRuntime
from friday.agent_runtime.llm import LLMRouter
from friday.api.conversations import router as conversations_router
from friday.api.deps import (
    _audit,
    _json_load,
    _parse_json_bool,
    _parse_json_float,
    _request_json,
    _require,
)
from friday.api.events import router as events_router
from friday.api.files import router as files_router
from friday.api.inbox import router as inbox_router
from friday.api.ingest import router as ingest_router
from friday.api.kg import router as kg_router
from friday.api.knowledge import router as knowledge_router
from friday.api.notifications import router as notifications_router
from friday.config import (
    FridaySettings,
    ensure_runtime_dirs,
    load_settings,
    validate_settings,
)
from friday.diagnostics.runtime_lease import ProcessLease
from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.executive.api import admin_router as missions_admin_router
from friday.executive.api import router as missions_router
from friday.ingestion import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IngestionPipeline,
)
from friday.knowledge_graph import KnowledgeGraph
from friday.memory import MemoryVault
from friday.organs import ServiceContext, build_registry
from friday.permissions import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthenticationError,
    AuthorizationError,
    AuthorizationService,
    bind_actor,
)
from friday.retrieval import EmbeddingBackend, HybridSearcher
from friday.retrieval._rerank_backend import RerankBackend, rerank_with_backend
from friday.security import verify_bridge_request
from friday.storage import init_storage, normalize_conversation_mode
from friday.storage.models import (
    AuditEntry,
    FeedbackType,
    new_id,
)
from friday.web_surfer import WebSurfer
from friday.workers import IntervalTask, WorkersManager
from friday.workers._blocking import run_blocking, wait_until_idle

LOGGER = logging.getLogger(__name__)
VERSION = __version__
_REALM_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
# What a client-proposed correlation id may look like: long enough for a UUID or
# a trace id, plain enough that it cannot smuggle a header or a log line.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestBodyTooLargeError(RuntimeError):
    """The ASGI request stream exceeded the configured hard byte limit."""


def _is_request_body_limit_exception(exc: BaseException) -> bool:
    if isinstance(exc, RequestBodyTooLargeError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_request_body_limit_exception(nested) for nested in exc.exceptions
        )
    return False


class RequestBodyLimitMiddleware:
    """Reject oversized bodies even when Transfer-Encoding is chunked.

    Authentication may need the exact body for HMAC verification, so this pure
    ASGI middleware deliberately sits outside authentication and counts bytes
    before any route or parser can buffer an unbounded stream.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"").strip()
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._reject(scope, receive, send, 400, "Invalid Content-Length")
                return
            if content_length < 0:
                await self._reject(scope, receive, send, 400, "Invalid Content-Length")
                return
            if content_length > self.max_bytes:
                await self._reject(scope, receive, send, 413, "Request body is too large")
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BaseException as exc:
            if not _is_request_body_limit_exception(exc):
                raise
            if response_started:
                # Routes read request bodies before emitting a response. This
                # branch is defensive for a future streaming endpoint where a
                # second response would violate ASGI.
                return
            await self._reject(scope, receive, send, 413, "Request body is too large")

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)


def _max_request_body_bytes(settings: FridaySettings) -> int:
    """Bound JSON/base64 and multipart requests from configured product limits."""

    base64_file = 4 * ((settings.max_upload_bytes + 2) // 3)
    utf8_text = 4 * settings.max_extracted_text_chars
    framing_allowance = 1024 * 1024
    return max(
        settings.max_upload_bytes + framing_allowance,
        base64_file + utf8_text + framing_allowance,
    )


class SlidingWindowLimiter:
    """Small per-process limiter for API and Telegram abuse protection."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, *, window_sec: float = 60.0) -> bool:
        now = time.monotonic()
        cutoff = now - window_sec
        async with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= max(1, limit):
                return False
            bucket.append(now)
            if len(self._events) > 10_000:
                stale = [name for name, values in self._events.items() if not values or values[-1] <= cutoff]
                for name in stale[:2_000]:
                    self._events.pop(name, None)
            return True

    async def exhausted(self, key: str, limit: int, *, window_sec: float = 60.0) -> bool:
        """True when the bucket is already full — a check that consumes nothing."""
        now = time.monotonic()
        cutoff = now - window_sec
        async with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            return len(bucket) >= max(1, limit)


def _client_ip(request: Request, settings: FridaySettings) -> str:
    """Return the effective client IP without trusting attacker-supplied headers.

    ``X-Forwarded-For`` is considered only when the immediate TCP peer belongs
    to an explicitly trusted proxy network. The chain is then evaluated from
    right to left, matching common reverse-proxy semantics.
    """
    host = request.client.host if request.client else ""
    if not settings.trust_proxy_headers:
        return host
    try:
        peer = ipaddress.ip_address(host)
        trusted = [ipaddress.ip_network(value, strict=False) for value in settings.trusted_proxy_networks]
    except ValueError:
        return host
    if not any(peer in network for network in trusted):
        return host

    forwarded = [
        item.strip() for item in request.headers.get("x-forwarded-for", "").split(",") if item.strip()
    ]
    if not forwarded:
        return host
    try:
        chain = [ipaddress.ip_address(item.strip("[]")) for item in forwarded]
    except ValueError:
        return host
    for address in reversed(chain):
        if not any(address in network for network in trusted):
            return str(address)
    # Every hop is trusted, so the chain contains no address this deployment
    # actually vouches for. Falling back to `chain[0]` returned the LEFTMOST entry
    # — the one the client writes itself — and it fed a security decision. The peer
    # is the only address observed rather than asserted.
    return host


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.casefold() in {"localhost", "testclient"}


# Hostnames a local browser legitimately uses to reach a loopback-bound API.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

# Голос короче этого — вопрос, произнесённый вслух, и его транскрипт становится
# текстом хода. Длиннее — диктовка: она остаётся материалом Inbox без ответа
# по существу. Три минуты — щедрая верхняя граница устного вопроса.
_VOICE_QUESTION_MAX_SEC = 180.0


def _request_hostname(value: str) -> str | None:
    """Lowercase hostname from a ``Host`` header or an ``Origin`` URL."""
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return urlsplit(candidate if "//" in candidate else f"//{candidate}").hostname
    except ValueError:
        return None


def _guard_loopback_browser_request(request: Request, settings: FridaySettings) -> None:
    """CSRF/DNS-rebinding guard for the credential-less loopback owner bypass.

    A browser on the owner's machine can be steered by remote pages: a
    cross-site page may fire no-preflight mutations at ``127.0.0.1`` and a
    DNS-rebound hostname resolves there while keeping a foreign ``Host``.
    The implicit-owner path therefore only accepts requests shaped like a
    local, same-origin client. Callers with explicit credentials (bearer
    token, bridge HMAC) authenticate earlier and are never affected, and
    non-browser clients such as curl send none of these headers.
    """
    host = _request_hostname(request.headers.get("host", ""))
    if host not in _LOOPBACK_HOSTNAMES:
        raise AuthorizationError("Loopback authentication requires a loopback Host header; use an API token")
    # Reads are checked too. GET/HEAD used to return here, on the grounds that
    # "cross-origin reads stay blocked by CORS" — but CORS blocks READING THE
    # RESPONSE, not sending the request. With the credential-less loopback bypass
    # enabled, any page the owner happens to open could fire owner-authority GETs
    # at 127.0.0.1: `/api/profile?synthesize=true` and `/api/reflection?...` run
    # the model over personal data, `/api/files/{id}` and the backup download
    # emit bytes and write audit rows, and all of it silently spends the owner's
    # own rate budget. The attacker not seeing the response does not make the
    # request harmless.
    #
    # Legitimate callers are unaffected: the admin UI is same-origin (browsers
    # send `Sec-Fetch-Site: same-origin`, and `Origin` when they send one at all
    # points at the loopback host), a typed URL is `Sec-Fetch-Site: none`, and
    # curl or the CLI send neither header. OPTIONS never reaches here — the
    # middleware answers preflight before authentication.
    origin = request.headers.get("origin", "").strip()
    if origin:
        if origin in settings.cors_origins or _request_hostname(origin) in _LOOPBACK_HOSTNAMES:
            return
        raise AuthorizationError("Cross-origin browser requests cannot use loopback authentication")
    fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise AuthorizationError("Cross-site browser requests cannot use loopback authentication")


def _telegram_user_id(settings: FridaySettings, external_id: str) -> str:
    realm = _REALM_RE.sub("_", settings.telegram_realm_id).strip("._-") or "telegram"
    return f"telegram:{realm}:{external_id}"


# Deliberately narrower than 'user': granted automatically to a stranger the
# owner never approved by chat id, so it excludes anything that spends the
# owner's resources unattended or reaches beyond the newcomer's own tenant.
# Capability choice — see FRIDAY_TELEGRAM_OPEN_REGISTRATION in .env.local and
# the config docstring for `telegram_open_registration`.
NEWCOMER_PRESET_CAPABILITIES = frozenset(
    {
        "chat.use",
        "search.use",
        "knowledge.read",
        "knowledge.create",
        "knowledge.edit",
        "inbox.read",
        "inbox.review",
        "kg.read",
        "kg.write",
        "files.upload",
        "files.read",
        "feedback.write",
        "conversations.read",
        "conversations.manage",
        "web.search",
        "web.fetch",
    }
)


def _notify_owners_of_self_registration(
    storage: Any,
    settings: FridaySettings,
    *,
    user_id: str,
    display_name: str,
    username: str,
) -> None:
    """Tell every configured owner chat that a stranger just self-registered.

    Open registration admits private chats the owner never listed by id; without
    this push the only way to notice is to open the admin user list. One row per
    owner chat, deduped on the new account so a supervisor restart does not spam.
    The body carries only identity (name/username/id) — never message content.
    """
    owner_chats = list(settings.telegram_owner_chat_ids or [])
    if not owner_chats:
        return
    name = display_name.strip()
    handle = username.strip().lstrip("@")
    if name and handle:
        who = f"{name} (@{handle})"
    elif name:
        who = name
    elif handle:
        who = f"@{handle}"
    else:
        who = user_id
    body = f"Новый пользователь самозарегистрировался: {who}. Аккаунт {user_id}, preset newcomer."
    # user_id column is FK to users(id). Owner chats from settings may never
    # have a matching telegram-derived row, but LEGACY_OWNER_USER_ID is always
    # provisioned at app boot. Dedup includes the owner chat so every owner
    # chat gets one copy without colliding on (user_id, dedup_key), and a
    # supervisor restart still cannot re-spam the same (newcomer, owner) pair.
    for owner_chat_id in owner_chats:
        storage.enqueue_notification(
            LEGACY_OWNER_USER_ID,
            str(owner_chat_id),
            body,
            kind="onboarding",
            dedup_key=f"onboarding:{user_id}:{owner_chat_id}",
        )


def _ensure_newcomer_preset(auth_service: Any, storage: Any) -> str:
    """Привести системный пресет «newcomer» к константе и вернуть его ключ.

    Раньше здесь стояла охрана `if not preset_exists(...)`, и это была не
    оптимизация, а тихая потеря контроля: набор писался в базу ОДИН раз, при
    первой в жизни установки саморегистрации, и после этого правка
    `NEWCOMER_PRESET_CAPABILITIES` в коде не решала ничего. Сужение прав новичка
    (а это единственный пресет, который выдаётся человеку с улицы автоматически)
    не доехало бы до боевой базы вовсе — притом что докстринг обещал обратное.

    Расхождение не применяется молча: если набор в базе отличается от
    константы, разница пишется событием. Ручная правка системного пресета через
    админку так и остаётся обнаружимой — она не исчезает бесследно, а видна в
    ленте событий как замещённая.
    """
    expected = set(NEWCOMER_PRESET_CAPABILITIES)
    current = storage.get_custom_preset("newcomer") or {}
    granted = set(current.get("capabilities") or [])
    if current and granted == expected:
        return "newcomer"
    storage.upsert_custom_preset(
        "newcomer",
        "Новичок (авторегистрация)",
        expected,
        description=(
            "Автоматически выдаётся при первом сообщении в личку, когда "
            "включена открытая регистрация (FRIDAY_TELEGRAM_OPEN_REGISTRATION). "
            "Чат, свои знания, файлы, веб-поиск — без миссий и выполнения кода."
        ),
        created_by="system",
    )
    if current:
        storage.record_event(
            "presets.newcomer_synced",
            {
                "revoked": sorted(granted - expected),
                "granted": sorted(expected - granted),
            },
        )
    return "newcomer"


def _chat_request_fingerprint(
    *,
    actor_source: str,
    bridge_chat_id: str,
    message: str,
    body: dict[str, Any],
    force_knowledge: bool,
    enable_tools: bool,
    attachments: list[dict[str, Any]],
    document: dict[str, Any] | None,
    document_digest: str,
) -> str:
    """Bind an idempotency key to the request that actually produced it.

    Large base64 payloads are represented by the digest of decoded bytes so
    fingerprinting remains bounded and exact retries remain deterministic.
    """
    document_fingerprint: dict[str, Any] | None = None
    if document is not None:
        document_fingerprint = {
            "filename": str(document.get("filename") or "telegram-file.bin"),
            "mime_type": str(document.get("mime_type") or "application/octet-stream"),
            "source_ref": str(document.get("source_ref") or "").strip()[:500],
            "content_sha256": document_digest,
        }
    canonical = {
        "actor_source": actor_source,
        "bridge_chat_id": bridge_chat_id,
        "message": message,
        "force_knowledge": force_knowledge,
        "enable_tools": enable_tools,
        "conversation_id": str(body.get("conversation_id") or "").strip(),
        "telegram_message_id": str(body.get("telegram_message_id") or ""),
        "attachments": attachments,
        "document": document_fingerprint,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audit_boundary_refusal(
    request: Request,
    reason: str,
    *,
    status: int,
    action: str = "auth.failed",
    actor: ActorContext | None = None,
) -> None:
    """Durably record a refusal at the HTTP boundary.

    Metadata only (reason code, status, method, path) — never the attempted
    secret — so a leaked token's abuse (or a brute-force) is forensically
    visible in the audit log. Best-effort: auditing a failure must never turn
    the failure response into a 500.

    `action` РАЗДЕЛЯЕТ два разных события, которые раньше писались одинаково.
    Замерено на живой базе: из 1302 записей `auth.failed` **1188** были
    `rate_limited` от владельца, пачкой разбиравшего Inbox с ВЕРНЫМ токеном с
    127.0.0.1. Троттлинг вошедшего пользователя — не отказ аутентификации, и
    смешивание стоило дважды: диагностика постоянно кричала «возможен брутфорс»
    (порог 60 за сутки), а три настоящих обращения с чужого адреса
    203.0.113.20 в `/api/admin/users` и `/api/admin/knowledge` лежали под
    этой лавиной невидимыми. Сигнал, который горит всегда, не читают.
    """
    actor = actor or getattr(request.state, "actor", None)
    with suppress(Exception):
        request.app.state.storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=getattr(actor, "user_id", None) or "anonymous",
                action=action,
                target_type="auth",
                target_id=reason,
                after_json={
                    "status": status,
                    "reason": reason,
                    "method": request.method,
                    "path": request.url.path,
                },
                ip_address=getattr(request.state, "client_ip", ""),
                request_id=getattr(request.state, "request_id", ""),
            )
        )


def _audit_auth_failure(request: Request, reason: str, *, status: int) -> None:
    """Отказ ИМЕННО аутентификации или авторизации — то, что считает диагностика."""
    _audit_boundary_refusal(request, reason, status=status, action="auth.failed")


def _bridge_header(request: Request, name: str) -> str:
    """Заголовок моста по новому имени, с приёмом прежнего.

    Мост и бэкенд — два ОТДЕЛЬНЫХ процесса и обновляются не одновременно: на
    время переименования (ex codename Jericho) один из них какое-то время шлёт
    старые заголовки. Отвергать их значило бы устроить себе тишину в чате ровно
    в момент обновления, причём молча — с виду это неотличимо от «бот умер».
    """
    return request.headers.get(f"x-friday-{name}") or request.headers.get(f"x-jericho-{name}") or ""


async def _authenticate(request: Request) -> ActorContext:
    state = request.app.state
    settings: FridaySettings = state.settings
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.casefold().startswith("bearer ") else ""
    signature = _bridge_header(request, "signature")

    if bearer and signature:
        raise AuthenticationError("Use either API-token or bridge authentication, not both")

    if signature:
        raw_body = await request.body()
        # The bridge signs the exact request target it sends, including any
        # query string, so verification must cover path?query as well —
        # otherwise query-bearing bridge GETs can never authenticate.
        signed_path = request.url.path
        if request.url.query:
            signed_path = f"{signed_path}?{request.url.query}"
        identity = verify_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=_bridge_header(request, "timestamp"),
            method=request.method,
            path=signed_path,
            external_user_id=_bridge_header(request, "user"),
            chat_id=_bridge_header(request, "chat"),
            nonce=_bridge_header(request, "nonce"),
            body=raw_body,
            signature=signature,
            max_age_sec=settings.telegram_signature_max_age_sec,
        )
        chat_number = int(identity.chat_id)
        # In Telegram a private chat's id equals the sender's — the same signal
        # `preset_for_new_account` uses below. Computed here, once, so the gate
        # and the preset choice can never disagree about what counts as private.
        sender_number = int(identity.external_user_id) if identity.external_user_id.isdigit() else 0
        in_private_chat = bool(sender_number) and chat_number == sender_number
        chat_is_allowlisted = chat_number in settings.telegram_effective_allowed_chat_ids
        # Deny-by-default: only chats on the effective allowlist (allowlist plus
        # owner chats) may authenticate. An empty allowlist denies every chat.
        # Raise AuthorizationError (403) so the bridge dead-letters the update
        # instead of retrying an unauthorized chat hundreds of times.
        #
        # The ONE exception is a private chat when open registration is on — the
        # bridge already let it through on the same basis (see `_commands.py`);
        # this is the backend's independent re-check of that same decision, not a
        # second, looser gate.
        if not chat_is_allowlisted and not (settings.telegram_open_registration and in_private_chat):
            raise AuthorizationError("Telegram chat is not allowed")
        # Single-use nonce closes the replay window inside the freshness bound.
        if not state.storage.claim_bridge_nonce(identity.nonce):
            raise AuthorizationError("Telegram request was already used")
        try:
            parsed = json.loads(raw_body or b"{}")
            if not isinstance(parsed, dict):
                parsed = {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        request.state.json_body = parsed
        telegram_user_value = parsed.get("telegram_user")
        telegram_user: dict[str, Any] = telegram_user_value if isinstance(telegram_user_value, dict) else {}
        display_name = " ".join(
            part.strip()
            for part in (
                str(telegram_user.get("first_name") or ""),
                str(telegram_user.get("last_name") or ""),
            )
            if part.strip()
        )
        # Кем вошли и чьи это данные — разные вопросы. Если владелец явно связал этот
        # телеграм со своим аккаунтом, арендатор берётся ОТТУДА; иначе всё как было —
        # производный идентификатор и автопровижн. Без связи вопрос из телеграма
        # искал в аккаунте, у которого нет ни одного документа, и получал честное
        # «ничего не нашлось» о корпусе, лежащем рядом под другим арендатором.
        derived_id = _telegram_user_id(settings, identity.external_user_id)
        linked_id = state.storage.resolve_identity("telegram", identity.external_user_id)
        user_id = linked_id or derived_id
        existing = state.storage.get_user(user_id)
        # Allowlisting a GROUP chat handed an account with the full 'user' preset to
        # every participant who wrote in it. Tenant isolation keeps the owner's
        # knowledge private, so the exposure is not exfiltration but spending the
        # owner's resources: that preset grants web search and fetch, file upload and
        # background missions. A NEW account created in a non-private chat therefore
        # gets 'guest' (read and chat only) — least privilege instead of a lockout, so
        # nobody in the chat stops working. In Telegram a private chat's id equals the
        # sender's, which is what distinguishes the two (`in_private_chat`, computed
        # above alongside the allowlist gate). ``ensure_user`` never rewrites an
        # existing preset, so the owner and anyone already provisioned are untouched.
        #
        # A private chat that reached this point only because of open registration
        # (not the static allowlist) is a stranger the owner never approved by id.
        # Handing them 'user' — web access, file upload, background missions running
        # on the owner's LLM budget — would make open registration a way to spend
        # that budget anonymously. `newcomer` is the deliberately narrower preset
        # decided for this feature: chat, own knowledge, files, web search; no
        # missions, no code execution. See `ensure_newcomer_preset` below.
        if in_private_chat and chat_is_allowlisted:
            preset_for_new_account = "user"
        elif in_private_chat:
            preset_for_new_account = _ensure_newcomer_preset(state.auth_service, state.storage)
        elif settings.telegram_group_members_full_access:
            preset_for_new_account = "user"
        else:
            preset_for_new_account = "guest"
        # `chat_id` is not bookkeeping: it is where every proactive organ delivers.
        # Recording the chat the user last wrote in meant one message sent in an
        # allowlisted GROUP redirected the weekly digest, reminders and "on this day"
        # — the owner's own knowledge — into that group, silently and permanently.
        # A push target has to be a chat the user is alone in, so only a private chat
        # updates it; from a group we leave whatever private chat is already on file
        # (ensure_user merges metadata rather than replacing it).
        metadata: dict[str, Any] = {"language_code": telegram_user.get("language_code")}
        if in_private_chat:
            metadata["chat_id"] = identity.chat_id
        if in_private_chat and not chat_is_allowlisted and settings.telegram_open_registration:
            # ФАКТ ВПУСКА, а не текущая роль. Проактивные органы должны понимать,
            # что этот чат впустил сам backend: гейт по пресету («сейчас
            # newcomer») отбирал у человека все уведомления в тот момент, когда
            # владелец повышал его до обычного пресета — то есть ровно за то, что
            # его признали своим. Признак пишется один раз и не меняется ролью.
            metadata["self_registered"] = True
        if linked_id:
            # Аккаунт уже существует и принадлежит человеку, а не этому каналу.
            # `ensure_user` переписал бы `source` на 'telegram' и `external_id` на
            # номер чата — то есть владелец, вошедший через бота, перестал бы
            # выглядеть владельцем в списке аккаунтов. Из телеграма сюда приходит
            # ровно одна полезная вещь — `chat_id`, куда доставляют проактивные
            # органы; её и записываем, остальное аккаунта не касается.
            # `source=""` обязателен: значение по умолчанию — 'local', а в UPDATE стоит
            # `CASE WHEN excluded.source<>''`, то есть дефолт молча переписал бы
            # 'api-token' владельца. Пустая строка — единственный способ сказать
            # «не трогай это поле». `preset_key` в UPDATE не участвует вовсе.
            state.storage.ensure_user(user_id, source="", metadata=metadata)
        else:
            state.storage.ensure_user(
                user_id,
                source="telegram",
                external_id=identity.external_user_id,
                display_name=display_name,
                username=str(telegram_user.get("username") or ""),
                preset_key=preset_for_new_account,
                metadata=metadata,
            )
            # First-time self-registration only: existing was None, no identity
            # link, and the account received the deliberately narrow newcomer
            # preset (private chat admitted solely by open registration). A
            # returning newcomer keeps existing set and never re-notifies.
            if existing is None and preset_for_new_account == "newcomer":
                _notify_owners_of_self_registration(
                    state.storage,
                    settings,
                    user_id=user_id,
                    display_name=display_name,
                    username=str(telegram_user.get("username") or ""),
                )
        user = state.storage.get_user(user_id) or existing or {}
        if user.get("status") != "active":
            raise AuthenticationError("User account is disabled")
        request.state.bridge_chat_id = identity.chat_id
        request.state.bridge_external_user_id = identity.external_user_id
        return state.auth_service.actor_for_user(
            user_id,
            source="telegram-bridge",
            identity_id=identity.external_user_id,
        )

    if bearer:
        # The configured owner token stays the all-capability owner (back-compat).
        # Bytes for the same reason as the bridge signature: an Authorization header
        # carrying an obs-text byte made `compare_digest` raise TypeError, and an
        # unauthenticated request answered 500 instead of 401 — unaudited, and outside
        # the failed-authentication budget.
        if settings.api_token and hmac.compare_digest(
            bearer.encode("utf-8"), settings.api_token.encode("utf-8")
        ):
            user = state.storage.ensure_user(
                LEGACY_OWNER_USER_ID,
                source="api-token",
                display_name="Owner",
                preset_key="owner",
            )
            if user.get("preset_key") != "owner":
                state.storage.update_user(LEGACY_OWNER_USER_ID, preset_key="owner")
            if user.get("status") != "active":
                raise AuthenticationError("Owner account is disabled")
            return ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token", identity_id="owner-token")
        # Otherwise resolve a scoped per-account token: the bearer authenticates as
        # its bound user with exactly that user's preset/capabilities, so the role
        # model finally applies to HTTP actors, not only Telegram users.
        token_hash = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
        token_row = state.storage.find_api_token(token_hash)
        if token_row is None:
            raise AuthenticationError("Invalid API token")
        token_user_id = str(token_row["user_id"])
        token_user = state.storage.get_user(token_user_id)
        if not token_user or token_user.get("status") != "active":
            raise AuthenticationError("Token account is disabled")
        state.storage.touch_api_token(str(token_row["id"]))
        return state.auth_service.actor_for_user(
            token_user_id,
            source="api-token",
            identity_id=str(token_row["id"]),
        )

    # The TCP PEER, not the forwarded chain. `X-Forwarded-For` is client-supplied
    # and may legitimately influence rate-limit attribution; it must never decide
    # authentication. Behind a trusted reverse proxy, a remote request carrying
    # `X-Forwarded-For: 127.0.0.1` otherwise resolved to loopback and took the
    # credential-less owner path.
    peer_ip = str(getattr(request.client, "host", "") or "")
    if not settings.api_require_token_on_loopback and _is_loopback(peer_ip):
        _guard_loopback_browser_request(request, settings)
        user = state.storage.ensure_user(
            LEGACY_OWNER_USER_ID,
            source="loopback",
            display_name="Owner",
            preset_key="owner",
        )
        if user.get("preset_key") != "owner":
            state.storage.update_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        # A disabled owner must stay disabled: the token path refuses it, and
        # the credential-less loopback path must never silently reactivate it.
        if user.get("status") != "active":
            raise AuthenticationError("Owner account is disabled")
        return ActorContext(LEGACY_OWNER_USER_ID, "owner", "loopback")
    raise AuthenticationError("Missing authentication")


async def _enforce_rate_limit(request: Request, actor: ActorContext) -> None:
    settings: FridaySettings = request.app.state.settings
    limiter: SlidingWindowLimiter = request.app.state.rate_limiter
    if actor.source == "telegram-bridge":
        if not await limiter.allow(
            "telegram:global",
            settings.telegram_global_rate_limit_per_minute,
        ):
            raise HTTPException(
                status_code=429,
                detail="Telegram bridge rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        if not await limiter.allow(
            f"telegram:user:{actor.user_id}",
            settings.telegram_user_rate_limit_per_minute,
        ):
            raise HTTPException(
                status_code=429,
                detail="User rate limit exceeded",
                headers={"Retry-After": "60"},
            )
    elif not await limiter.allow(
        f"api:user:{actor.user_id}",
        settings.api_user_rate_limit_per_minute,
    ):
        raise HTTPException(
            status_code=429,
            detail="API rate limit exceeded",
            headers={"Retry-After": "60"},
        )


def create_app(settings_override: FridaySettings | None = None) -> FastAPI:
    settings = settings_override or load_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        ensure_runtime_dirs(settings)
        # SQLite protects individual transactions, but two full API runtimes
        # would still duplicate workers, scheduled backups, and side effects.
        # Keep one backend role per state directory and fail fast with useful
        # lock metadata instead of running a split-brain installation.
        with ProcessLease(
            settings.state_dir / "backend.lock",
            protocol="friday.backend.v1",
        ):
            storage = init_storage(settings)
            storage.ensure_user(
                LEGACY_OWNER_USER_ID,
                source="api-token",
                display_name="Owner",
                preset_key="owner",
            )
            storage.update_user(LEGACY_OWNER_USER_ID, preset_key="owner", status="active")
            auth_service = AuthorizationService(storage)
            llm = LLMRouter(settings)
            embeddings = EmbeddingBackend(settings)
            # Переранжировщик подключается, только если настроен адрес И задана
            # глубина: два условия, потому что поднять службу и забыть включить шаг —
            # ровно та же ошибка, что включить шаг без службы, и обе молчаливые.
            rerank_backend = RerankBackend(settings)
            reranker = None
            if rerank_backend.enabled and settings.rerank_top > 0:
                reranker = functools.partial(rerank_with_backend, rerank_backend)
                LOGGER.info("reranking enabled: model %s, top %d", settings.rerank_model, settings.rerank_top)
            searcher = HybridSearcher(
                storage,
                embeddings,
                graph_max_depth=settings.graph_max_depth,
                pool_max=settings.retrieval_pool_max,
                dense_evidence_min=settings.retrieval_dense_evidence_min,
                reranker=reranker,
                rerank_top=settings.rerank_top,
                rerank_confident_min=settings.rerank_confident_min,
            )
            graph = KnowledgeGraph(storage)
            ingestion = IngestionPipeline(settings, storage, graph, llm)
            web_surfer = WebSurfer(settings)
            kernel = ExecutionKernel(auth_service, settings)
            kernel.bind_services(storage, graph, web_surfer, ingestion, searcher=searcher)
            agent = AgentRuntime(settings, storage, llm, kernel)
            executive = ExecutiveService(settings, storage, auth_service, kernel, llm, ingestion)
            kernel.bind_executive(executive)
            memory_vault = MemoryVault(settings.memory_vault_dir)

            # Organs (JOP): register their capabilities, mount their routers, and
            # feed their background workers into the supervisor. All additive.
            organs = build_registry(settings)
            for capability in organs.capabilities():
                auth_service.register_capability(capability)
            organ_ctx = ServiceContext(
                settings=settings,
                storage=storage,
                kg=graph,
                ingestion=ingestion,
                llm=llm,
                auth=auth_service,
            )
            organ_workers = [
                IntervalTask(
                    name=worker.name,
                    func=functools.partial(worker.run, organ_ctx),
                    interval_sec=worker.interval_sec,
                    enabled=worker.enabled,
                    run_immediately=worker.run_immediately,
                    timeout_sec=worker.timeout_sec,
                )
                for worker in organs.workers(organ_ctx)
            ]

            workers = WorkersManager(
                settings,
                storage,
                ingestion,
                graph,
                memory_vault,
                llm,
                executive,
                embeddings=embeddings,
                extra_workers=organ_workers,
            )

            application.state.settings = settings
            application.state.storage = storage
            application.state.auth_service = auth_service
            application.state.llm = llm
            application.state.embeddings = embeddings
            application.state.hybrid_searcher = searcher
            application.state.kg = graph
            application.state.ingestion = ingestion
            application.state.web_surfer = web_surfer
            application.state.kernel = kernel
            application.state.agent = agent
            application.state.executive = executive
            application.state.memory_vault = memory_vault
            application.state.workers = workers
            application.state.organs = organs
            application.state.rate_limiter = SlidingWindowLimiter()
            await workers.start()
            LOGGER.info("Friday API started on %s:%s", settings.api_host, settings.api_port)
            try:
                yield
            finally:
                await workers.stop()
                await web_surfer.close()
                # workers.stop() only cancels the asyncio tasks; a worker cancelled
                # while awaiting asyncio.to_thread(storage.<db op>) leaves that call
                # still running on the default executor thread.
                #
                # The drain is bounded by the WORKERS' own budget, not by a constant.
                # A flat 30 s was shorter than what a worker is allowed to hold a
                # thread for — `knowledge_dedup` scans for up to 600 s inside a 900 s
                # tick — so shutdown routinely gave up while the work was legitimately
                # still running. And `storage.close()` does not cover it either: it
                # takes the write lock, while the read path deliberately takes no lock
                # at all, so a thread halfway through a SELECT is invisible to it.
                drain_budget = workers.max_timeout_sec + 30.0
                stranded = await asyncio.to_thread(wait_until_idle, drain_budget)
                if stranded:
                    LOGGER.warning(
                        "Shutting down with %s still executing after %.0fs; "
                        "their connections will be closed underneath them",
                        stranded,
                        drain_budget,
                    )
                with suppress(Exception):
                    # shutdown_default_executor's own timeout argument is 3.12+ and
                    # requires-python is >=3.11, hence wait_for.
                    await asyncio.wait_for(
                        asyncio.get_running_loop().shutdown_default_executor(),
                        timeout=max(30.0, drain_budget),
                    )
                # `final=True`: anything that still outlives this gets a loud
                # StorageClosedError rather than a fresh connection to a database
                # whose process lease is about to be released.
                storage.close(final=True)
                LOGGER.info("Friday API stopped")

    # The schema is behind a capability, so FastAPI's built-in (authenticated but
    # ungated) routes are switched off and re-served below. Authentication alone
    # was not enough: the guest preset is created automatically for anyone who
    # writes in an allow-listed GROUP chat, and it could read the full inventory
    # of routes, parameters and admin endpoints — a map of everything its own
    # preset is forbidden to call.
    application = FastAPI(
        title="Friday API",
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = settings
    application.state.rate_limiter = SlidingWindowLimiter()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Friday-Timestamp",
            "X-Friday-User",
            "X-Friday-Chat",
            "X-Friday-Signature",
            "X-Request-ID",
        ],
    )

    @application.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        path = request.url.path
        # A client may propose a correlation id, but only in a shape that cannot
        # forge evidence. This value is written into every audit row for the
        # request and echoed back in the response, and it was taken verbatim: a
        # caller could stamp their own writes with somebody else's id, or with
        # kilobytes of junk, in the exact field an investigator uses to tie events
        # together. Anything that is not a short, plain token is replaced rather
        # than rejected — correlation is a convenience, not a reason to fail a
        # legitimate request.
        proposed = str(request.headers.get("x-request-id") or "")
        request.state.request_id = proposed if _REQUEST_ID_RE.fullmatch(proposed) else secrets.token_hex(12)
        request.state.client_ip = _client_ip(request, settings)
        # `/health` — публичный синоним `/api/health`. Не удобство: маршрута с таким
        # именем не было, а проверка подлинности идёт РАНЬШЕ маршрутизации, поэтому
        # обращение к нему возвращало 401 и писалось в журнал как отказ
        # аутентификации. Замерено: 89 таких записей, все с 127.0.0.1 — собственный
        # smoke-check рестарта, тот самый, что записан в runbook проекта. Ничего
        # нового наружу не открывается: `/api/health` публичен ровно так же.
        public = (
            path == "/"
            or path == "/api/health"
            or path == "/health"
            or path == "/admin"
            or path.startswith("/admin/")
        )
        if request.method == "OPTIONS" or public:
            response = await call_next(request)
        else:
            # Failed authentication is rate-limited per client IP so bearer
            # tokens and bridge signatures cannot be brute-forced: once the
            # failure budget is spent, credentials are not even evaluated.
            limiter: SlidingWindowLimiter = request.app.state.rate_limiter
            failure_key = f"auth-fail:{request.state.client_ip}"
            failure_limit = settings.api_auth_failure_limit_per_minute

            async def _count_auth_failure() -> None:
                await limiter.allow(failure_key, failure_limit)

            # The budget is spent by FAILURES and it gates FAILURES — it is read
            # here and acted on only in the handlers below.
            #
            # It used to short-circuit before `_authenticate`, so a spent budget
            # refused valid credentials too. The budget is keyed on the client IP,
            # and on the default single-host deployment the admin UI, the CLI and
            # the Telegram bridge all arrive from 127.0.0.1 — so ten credential-less
            # requests from a browser tab took the whole API offline for a minute,
            # for every caller, including the owner. A cross-origin page can send
            # exactly those requests (`fetch(url, {mode: 'no-cors'})` — a simple GET,
            # no preflight), which made it a drive-by outage.
            #
            # Verifying credentials first costs one HMAC or one indexed token
            # lookup per request, and brute force is unaffected: guessing means
            # failing, failures still spend the budget, and once it is spent every
            # failure is answered with 429 instead of 401.
            budget_spent = await limiter.exhausted(failure_key, failure_limit)

            def _auth_failure_response(detail: str) -> JSONResponse:
                if budget_spent:
                    _audit_auth_failure(request, "rate_limited", status=429)
                    return JSONResponse(
                        {"detail": "Too many failed authentication attempts"},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                return JSONResponse({"detail": detail}, status_code=401)

            # Only the credential phase is guarded here. ``call_next`` used to sit
            # inside this block, which meant any ValueError escaping a route handler
            # came back as 401 "malformed credentials" AND spent the per-IP
            # auth-failure budget — a handler bug locked the owner out of their own
            # instance with 429. Route-raised exceptions belong to FastAPI's own
            # handlers (registered below) and to ServerErrorMiddleware.
            actor = None
            try:
                actor = await _authenticate(request)
                await _enforce_rate_limit(request, actor)
            except AuthenticationError as exc:
                await _count_auth_failure()
                if not budget_spent:
                    _audit_auth_failure(request, "invalid_credentials", status=401)
                response = _auth_failure_response(str(exc))
            except AuthorizationError as exc:
                _audit_auth_failure(request, "capability_denied", status=403)
                response = JSONResponse({"detail": str(exc)}, status_code=403)
            except HTTPException as exc:
                # Exceptions raised before ``call_next`` are outside FastAPI's
                # route exception handlers.  Convert them here so middleware
                # checks such as rate limiting cannot accidentally surface as
                # an internal server error.
                if exc.status_code == 429:
                    # Сюда 429 приходит ТОЛЬКО из `_enforce_rate_limit`, а он вызван
                    # строкой ниже `_authenticate`, то есть у запроса уже есть
                    # действительный актор. `_authenticate` своих HTTPException не
                    # бросает — только AuthenticationError и AuthorizationError,
                    # перехваченные выше. Это придержанный СВОЙ, а не чужой.
                    #
                    # И записывается он ИМЕНЕМ: `request.state.actor` к этому моменту
                    # ещё не привязан, поэтому событие уходило как `anonymous` — а
                    # смысл записи ровно в том, что мы знаем, кого придержали. На
                    # нескольких пользователях без имени она бесполезна.
                    _audit_boundary_refusal(
                        request,
                        "rate_limited",
                        status=429,
                        action="request.throttled",
                        actor=actor,
                    )
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            except ValueError as exc:
                # Malformed credentials (bad timestamps, chat ids, …) are
                # authentication failures too and consume the same budget.
                await _count_auth_failure()
                if not budget_spent:
                    _audit_auth_failure(request, "malformed_credentials", status=401)
                response = _auth_failure_response(str(exc))
            else:
                # `else`, not a trailing block: the handlers above must not see
                # anything ``call_next`` raises, and the credentials are known good
                # exactly here.
                request.state.actor = actor
                with bind_actor(actor):
                    response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Strict CSP: the admin UI ships external app.js/app.css and delegated
        # event handlers, so no 'unsafe-inline' allowance is needed anywhere.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    # Added after the decorator middleware so Starlette places this limiter
    # outside authentication; signed bridge bodies are bounded before HMAC
    # verification buffers them.
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=_max_request_body_bytes(settings),
    )

    @application.exception_handler(AuthorizationError)
    async def authorization_error(_: Request, exc: AuthorizationError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @application.exception_handler(AuthenticationError)
    async def authentication_error(_: Request, exc: AuthenticationError):
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @application.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(_: Request, exc: IdempotencyConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(IdempotencyInProgressError)
    async def idempotency_in_progress(_: Request, exc: IdempotencyInProgressError):
        return JSONResponse(
            {"detail": str(exc)},
            status_code=409,
            headers={"Retry-After": "2"},
        )

    @application.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/admin/")

    @application.get("/health", include_in_schema=False)
    @application.get("/api/health", tags=["system"])
    async def health(request: Request) -> dict[str, Any]:
        storage = getattr(request.app.state, "storage", None)
        return {
            "status": "ok" if storage is not None else "starting",
            "version": VERSION,
            "llm_enabled": settings.llm_enabled,
            "model": settings.llm_model,
            "profile": settings.profile.name,
        }

    @application.get("/api/me", tags=["identity"])
    async def me(request: Request) -> dict[str, Any]:
        actor = request.state.actor
        user = request.app.state.storage.get_user(actor.user_id)
        return {
            "actor": {
                "user_id": actor.user_id,
                "preset_key": actor.preset_key,
                "source": actor.source,
            },
            "user": user,
            "capabilities": request.app.state.kernel.get_tool_names(actor),
        }

    # Одна общая граница: только СВОЙ аккаунт, никогда чужой user_id из тела или
    # запроса — вся защита от межарендаторной записи в том, что здесь нечего
    # спутать. Гейт — chat.use, а не что-то административное: это личная
    # настройка, ею должен уметь пользоваться и 'newcomer', и 'guest'.
    MAX_CUSTOM_INSTRUCTIONS_CHARS = 500

    @application.patch("/api/me/instructions", tags=["identity"])
    async def set_my_instructions(request: Request) -> dict[str, Any]:
        """Короткое пожелание о СТИЛЕ ответов, которое человек пишет себе сам.

        Не факт о пользователе (это `user_model`, выводится из его же базы) и не
        разрешение — тот же недоверенный конверт, что и весь остальной
        `context_payload`: правило «строки контекста — данные, не команды»
        (`SYSTEM_PROMPT`) защищает и это поле, попытка дописать в него «игнорируй
        предыдущие инструкции» ничего не даёт.

        Пусто — то же самое, что снять настройку: нет отдельной ручки «удалить»,
        потому что нечего удалять отдельно от значения.
        """
        actor = _require(request, "chat.use")
        body = await _request_json(request)
        text = " ".join(str(body.get("instructions") or "").split())[:MAX_CUSTOM_INSTRUCTIONS_CHARS]
        state = request.app.state
        user = state.storage.get_user(actor.user_id) or {}
        try:
            metadata = json.loads(str(user.get("metadata_json") or "{}"))
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if text:
            metadata["custom_instructions"] = text
        else:
            metadata.pop("custom_instructions", None)
        state.storage.update_user(actor.user_id, metadata_json=metadata)
        return {"custom_instructions": text}

    # Поиск по своей переписке (не knowledge_objects). Self-service контур как
    # /api/me/instructions: chat.use, только actor.user_id, без foreign user_id.
    @application.get("/api/me/messages/search", tags=["chat"])
    async def search_my_messages(
        request: Request,
        q: str = Query("", max_length=2000),
        limit: int = Query(20, ge=1, le=100),
        conversation_id: str | None = Query(None, max_length=128),
    ) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        state = request.app.state
        rows = state.storage.search_messages(
            actor.user_id,
            q,
            limit=limit,
            conversation_id=conversation_id,
        )
        return {
            "count": len(rows),
            "query": " ".join((q or "").split()).strip(),
            "results": [
                {
                    "id": str(row.get("id") or ""),
                    "conversation_id": str(row.get("conversation_id") or ""),
                    "role": str(row.get("role") or ""),
                    "content": str(row.get("content") or ""),
                    "created_at": row.get("created_at"),
                }
                for row in rows
            ],
        }

    # «Ещё раз» для последнего вопроса человека: тот же self-service контур, что
    # /api/me/instructions (chat.use, только свой аккаунт). Хранилище не умеет
    # ветвление ответов — agent.chat допишет новый user+assistant ход с тем же
    # текстом; вложения первого хода не переотправляются (осознанное упрощение G15),
    # но если у исходного хода они были — в ответе явная пометка (G17b).
    # Гонка двух /regenerate закрыта idempotency_claim по разговору+user-ходу (G17a).
    @application.post("/api/me/regenerate", tags=["chat"])
    async def regenerate_last_turn(request: Request) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        state = request.app.state
        body = await _request_json(request)
        conversation_id = str(body.get("conversation_id") or "").strip() or None
        channel_chat_id = getattr(request.state, "bridge_chat_id", None)
        if actor.source == "telegram-bridge" and channel_chat_id:
            session = state.storage.get_channel_session(
                actor.user_id,
                "telegram",
                str(channel_chat_id),
            )
            if session and not session.get("is_archived"):
                conversation_id = str(session["conversation_id"])
        if not conversation_id:
            raise HTTPException(
                status_code=400,
                detail="Нет активного разговора для повтора",
            )
        if not state.storage.get_conversation(conversation_id, actor.user_id):
            raise HTTPException(
                status_code=400,
                detail="Разговор не найден",
            )
        # Хвост из 4: обычно user+assistant (+ещё пара). Берём ПОСЛЕДНЕЕ user —
        # не «первое в окне», иначе два user подряд без ответа дали бы старый вопрос.
        recent = state.storage.get_conversation_messages(
            conversation_id,
            user_id=actor.user_id,
            limit=4,
        )
        last_user: dict[str, Any] | None = None
        for row in reversed(recent):
            if str(row.get("role") or "") == "user":
                last_user = row
                break
        if last_user is None:
            raise HTTPException(
                status_code=400,
                detail="В разговоре нет вопроса для повтора",
            )
        message = str(last_user.get("content") or "").strip()
        if not message:
            raise HTTPException(
                status_code=400,
                detail="В разговоре нет вопроса для повтора",
            )
        last_meta = _json_load(last_user.get("metadata_json"), {})
        had_attachments = bool(last_meta.get("had_attachments"))
        # Ключ включает id user-хода: concurrent double-tap на ОДИН ход дедупится,
        # а повторный /regenerate после успешного (новый user-ряд с новым id) — нет.
        request_key = f"regenerate:{conversation_id}:{last_user.get('id') or ''}"
        claim = state.storage.idempotency_claim(
            actor.user_id,
            request_key,
            lease_seconds=90,
        )
        if claim["status"] == "replay":
            cached = claim.get("response") or {}
            return {**cached, "idempotent_replay": True}
        if claim["status"] == "conflict":
            raise HTTPException(
                status_code=409,
                detail="regenerate already bound to a different request",
            )
        if claim["status"] == "in_progress":
            raise HTTPException(
                status_code=409,
                detail="Этот ответ уже перегенерируется",
                headers={"Retry-After": "2"},
            )
        lease_token = str(claim.get("lease_token") or "")
        try:
            result = await state.agent.chat(
                actor.user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                attachments=[],
                enable_tools=True,
                kg=state.kg,
                hybrid_searcher=state.hybrid_searcher,
                ingestion_result=None,
                # Повтор не превращает сгенерированный текст в вопрос человека:
                # признак берётся с самого хода, а не выводится заново из его
                # букв. Иначе «Загружен документ: с кем работал иван отчёт.pdf»
                # на повторе получал бы графовое расширение, которого первый ход
                # не получал, — при одном и том же тексте.
                synthetic_document_notice=bool(last_meta.get("synthetic_document_notice")),
            )
            if had_attachments:
                # Вложения не переигрываются: transient-файлы физически негде
                # взять, а документ без подписи дал бы «Загружен документ» без
                # байтов. Сказать об этом явно — иначе «ещё раз» выглядит как
                # полноценный переответ на том же основании.
                notice = (
                    "Ответ восстановлен без исходного вложения — модель не видит "
                    "файл, на котором строился первый ответ. Пришлите вложение "
                    "заново, если оно нужно для ответа."
                )
                result = {**result, "regenerate_notice": notice}
                # Как grounding_warning: Telegram ставит оговорку ПЕРЕД текстом.
                if not str(result.get("grounding_warning") or "").strip():
                    result["grounding_warning"] = notice
            if actor.source == "telegram-bridge" and channel_chat_id:
                state.storage.set_channel_conversation(
                    actor.user_id,
                    "telegram",
                    str(channel_chat_id),
                    result["conversation_id"],
                    mode=str(result.get("context", {}).get("interaction_mode") or "dialogue"),
                )
            if not state.storage.idempotency_complete(actor.user_id, request_key, lease_token, result):
                raise RuntimeError("Lost regenerate idempotency lease before response commit")
            return result
        except BaseException:
            if lease_token:
                state.storage.idempotency_release(actor.user_id, request_key, lease_token)
            raise

    # G19: предстоящие напоминания (entity_time → outbound_notifications kind=reminder).
    # Self-service как /api/me/instructions: chat.use, только actor.user_id.
    # Снятие = status='dismissed' БЕЗ очистки dedup_key — иначе scan_reminders
    # заново встанет в очередь через INSERT OR IGNORE.
    @application.get("/api/me/reminders", tags=["chat"])
    async def list_my_reminders(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        rows = request.app.state.storage.list_pending_reminders(actor.user_id, limit=limit)
        return {
            "count": len(rows),
            "items": [
                {
                    "id": str(row.get("id") or ""),
                    "body": str(row.get("body") or ""),
                    "dedup_key": str(row.get("dedup_key") or ""),
                    "created_at": row.get("created_at"),
                    "chat_id": str(row.get("chat_id") or ""),
                }
                for row in rows
            ],
        }

    @application.post("/api/me/reminders/{notification_id}/dismiss", tags=["chat"])
    async def dismiss_my_reminder(request: Request, notification_id: str) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        notification_id = str(notification_id or "").strip()
        if not notification_id:
            raise HTTPException(status_code=404, detail="Напоминание не найдено")
        ok = request.app.state.storage.dismiss_notification(actor.user_id, notification_id)
        if not ok:
            # 404, не 403: чужой id и уже снятый/отправленный выглядят одинаково —
            # не подтверждаем существование чужой очереди.
            raise HTTPException(status_code=404, detail="Напоминание не найдено")
        return {"dismissed": True, "id": notification_id}

    @application.get("/api/me/monitors", tags=["chat"])
    async def list_my_monitors(request: Request) -> dict[str, Any]:
        """Свои мониторы — сохранённые вопросы, за которыми система следит сама.

        Self-service под `chat.use`, как напоминания: монитор смотрит СВОЙ корпус
        владельца и сообщает ему же, поэтому отдельной способности не нужно —
        новая способность здесь означала бы, что за человека решает кто-то ещё.
        """
        actor = _require(request, "chat.use")
        rows = await run_blocking(request.app.state.storage.list_monitors, actor.user_id)
        return {"count": len(rows), "items": rows}

    @application.post("/api/me/monitors", tags=["chat"])
    async def create_my_monitor(request: Request) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        body = await _request_json(request)
        chat_id = str(getattr(request.state, "bridge_chat_id", "") or "")
        try:
            monitor = await run_blocking(
                request.app.state.storage.create_monitor,
                actor.user_id,
                str(body.get("query") or ""),
                chat_id=chat_id,
            )
        except ValueError as exc:
            # Два разных отказа, и человеку они читаются по-разному: слишком
            # короткое условие и исчерпанный потолок слежений.
            detail = (
                "Слишком много слежений — снимите лишние в /watching"
                if "много" in str(exc)
                else "Слишком короткий запрос для монитора"
            )
            raise HTTPException(status_code=400, detail=detail) from exc
        _audit(request, "monitor.create", "monitor", monitor.get("id"), after=monitor)
        return {"monitor": monitor}

    @application.post("/api/me/monitors/{monitor_id}/stop", tags=["chat"])
    async def stop_my_monitor(request: Request, monitor_id: str) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        stopped = await run_blocking(
            request.app.state.storage.stop_monitor, str(monitor_id or ""), actor.user_id
        )
        if not stopped:
            # 404, а не 403: чужой и уже снятый выглядят одинаково — существование
            # чужого монитора не подтверждается.
            raise HTTPException(status_code=404, detail="Монитор не найден")
        _audit(request, "monitor.stop", "monitor", monitor_id)
        return {"stopped": True, "id": monitor_id}

    @application.get("/api/me/approvals", tags=["chat"])
    async def list_my_approvals(request: Request) -> dict[str, Any]:
        """Свои заявки на подтверждение опасного действия (спека v3 §5).

        Под `chat.use`, как мониторы и напоминания: это СВОИ решения о СВОИХ
        данных. Отдельная способность означала бы, что подтверждать твои действия
        может кто-то другой, — а весь смысл механизма в обратном.

        `uncertain` показывается наравне с ожидающими намеренно: это исход, про
        который никто не знает, случился ли эффект, и висеть невидимым он не должен.
        """
        actor = _require(request, "chat.use")
        status = str(request.query_params.get("status") or "pending").strip() or None
        rows = await run_blocking(
            request.app.state.storage.list_action_approvals,
            actor.user_id,
            status=status,
            limit=int(request.query_params.get("limit") or 20),
        )
        total = await run_blocking(
            request.app.state.storage.count_action_approvals, actor.user_id, status=status
        )
        return {"count": len(rows), "total": total, "status": status, "items": rows}

    @application.post("/api/approvals/{approval_id}/decide", tags=["chat"])
    async def decide_my_approval(request: Request, approval_id: str) -> dict[str, Any]:
        """Решение человека — и, если это «да», немедленное исполнение.

        Исполнение стоит ЗДЕСЬ, а не отдельным вызовом, по той же причине, по
        которой заявление атомарно: между «человек согласился» и «действие
        случилось» не должно быть места, где всё замирает навсегда. Ошибка
        исполнения при этом не отменяет решения — она записывается в саму заявку.
        """
        actor = _require(request, "chat.use")
        body = await _request_json(request)
        decision = str(body.get("decision") or "").strip().casefold()
        if decision not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="Решение должно быть approve или reject")
        decided = await run_blocking(
            request.app.state.storage.decide_action_approval,
            str(approval_id or ""),
            actor.user_id,
            decision=decision,
            decided_by=actor.user_id,
        )
        if not decided:
            # Одинаковый ответ на «нет такой», «чужая» и «уже решена»: существование
            # чужой заявки не подтверждается, а повторное нажатие кнопки в чате —
            # обычное дело и не должно выглядеть поломкой.
            raise HTTPException(status_code=404, detail="Заявка не найдена или уже решена")
        _audit(request, f"approval.{decision}", "action_approval", approval_id, after=decided)
        if decision == "reject":
            return {"approval": decided, "executed": False}
        result = await request.app.state.kernel.execute_approved(str(approval_id), actor=actor)
        return {
            "approval": await run_blocking(
                request.app.state.storage.get_action_approval, str(approval_id), actor.user_id
            ),
            "executed": bool(result.success),
            "error": result.error,
        }

    @application.post("/api/chat", tags=["chat"])
    async def chat(request: Request) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        state = request.app.state
        body = await _request_json(request)
        force_knowledge = _parse_json_bool(
            body.get("force_knowledge"), field="force_knowledge", default=False
        )
        enable_tools = _parse_json_bool(body.get("enable_tools"), field="enable_tools", default=True)
        try:
            requested_mode = (
                normalize_conversation_mode(str(body["mode"])) if body.get("mode") is not None else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        message = str(body.get("message") or body.get("caption") or "").strip()
        # «Текст сочинил backend» и «файл уже принят отдельно» — разные факты; см.
        # разбор ниже, где они расходятся у голосового вопроса.
        synthetic_document_notice = False
        file_already_ingested = False
        attachments_value = body.get("attachments")
        attachments: list[dict[str, Any]] = (
            [dict(item) for item in attachments_value if isinstance(item, dict)]
            if isinstance(attachments_value, list)
            else []
        )
        document_value = body.get("document")
        document: dict[str, Any] | None = document_value if isinstance(document_value, dict) else None
        forward_value = body.get("forward")
        forward_meta: dict[str, Any] = forward_value if isinstance(forward_value, dict) else {}
        file_content: bytes | None = None
        document_digest = ""
        if document:
            encoded = str(document.get("content_base64") or "")
            try:
                file_content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=400, detail="Invalid document base64") from exc
            if len(file_content) > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="File is too large")
            document_digest = hashlib.sha256(file_content).hexdigest()
        source_ref = str(body.get("source_ref") or "").strip()[:500]
        if not source_ref and document:
            source_ref = str(document.get("source_ref") or "").strip()[:500]
        if actor.source == "telegram-bridge" and not source_ref:
            message_id = str(body.get("telegram_message_id") or "")
            source_ref = (
                f"telegram:{getattr(request.state, 'bridge_chat_id', '')}:{message_id}" if message_id else ""
            )
        if not message and not document:
            raise HTTPException(status_code=400, detail="message or document is required")
        if len(message) > settings.max_extracted_text_chars:
            raise HTTPException(status_code=413, detail="Message is too long")
        explicit_no_save = bool(
            message
            and "explicit_no_save"
            in state.ingestion.assess_text(
                message,
                force_knowledge=force_knowledge,
            ).penalties
        )

        # Claim the key atomically before any side effect. A plain lookup followed
        # by a later insert allows concurrent retries to invoke the agent twice.
        lease_token = ""
        heartbeat: asyncio.Task[None] | None = None
        if source_ref:
            request_hash = _chat_request_fingerprint(
                actor_source=actor.source,
                bridge_chat_id=str(getattr(request.state, "bridge_chat_id", "")),
                message=message,
                body=body,
                force_knowledge=force_knowledge,
                enable_tools=enable_tools,
                attachments=attachments,
                document=document,
                document_digest=document_digest,
            )
            claim = state.storage.idempotency_claim(
                actor.user_id,
                source_ref,
                request_hash=request_hash,
                lease_seconds=120,
            )
            if claim["status"] == "replay":
                cached = claim.get("response") or {}
                return {**cached, "idempotent_replay": True}
            if claim["status"] == "conflict":
                raise HTTPException(
                    status_code=409,
                    detail="source_ref is already bound to a different request",
                )
            if claim["status"] == "in_progress":
                raise HTTPException(
                    status_code=409,
                    detail="Request with this source_ref is already being processed",
                    headers={"Retry-After": "2"},
                )
            lease_token = str(claim.get("lease_token") or "")

            async def renew_lease() -> None:
                while True:
                    await asyncio.sleep(30)
                    if not state.storage.idempotency_renew(actor.user_id, source_ref, lease_token):
                        return

            heartbeat = asyncio.create_task(renew_lease(), name="jericho-idempotency-heartbeat")

        try:
            file_ingestion = None
            if document:
                state.auth_service.require(actor, "files.upload")
                if file_content is None:  # pragma: no cover - narrowed by document validation above
                    raise RuntimeError("Validated document bytes are unavailable")
                filename = str(document.get("filename") or "telegram-file.bin")
                mime_type = str(document.get("mime_type") or "application/octet-stream")
                if explicit_no_save:
                    transient_file = await state.ingestion.inspect_file_transient(
                        file_content,
                        filename=filename,
                        mime_type=mime_type,
                    )
                    attachments.append(
                        {
                            "filename": transient_file["filename"],
                            "transient": True,
                            "transient_text": transient_file["text_preview"],
                            "extraction_success": transient_file["extraction_success"],
                        }
                    )
                    file_ingestion = {
                        key: value for key, value in transient_file.items() if key != "text_preview"
                    }
                    file_ingestion.update(
                        {
                            "promoted": False,
                            "queued_for_review": False,
                            "action": "transient",
                            "reason": "explicit no-save request",
                            "raw_object_id": None,
                        }
                    )
                else:
                    media_kind = str(document.get("media_kind") or "")
                    file_metadata: dict[str, Any] = {
                        "channel": actor.source,
                        "chat_id": getattr(request.state, "bridge_chat_id", ""),
                    }
                    if media_kind:
                        file_metadata["media_kind"] = media_kind
                    if isinstance(document.get("duration"), int):
                        file_metadata["duration_sec"] = document["duration"]
                    if forward_meta:
                        file_metadata["forward"] = forward_meta
                    file_ingestion = await state.ingestion.ingest_file(
                        actor.user_id,
                        None,
                        file_content,
                        filename=filename,
                        mime_type=mime_type,
                        media_kind=media_kind,
                        metadata=file_metadata,
                        source_ref=str(document.get("source_ref") or source_ref or ""),
                    )
                    attachments.append(
                        {
                            "filename": filename,
                            "knowledge_object_id": (file_ingestion.get("knowledge_object") or {}).get("id"),
                        }
                    )
                # Голосовое сообщение — обычно ВОПРОС, произнесённый вслух.
                # Транскрипт считался и раньше (файл ждёт разбора в Inbox), но ходу
                # разговора не доставался: модель отвечала, видя лишь имя .ogg-файла,
                # а retrieval искал по строке «Загружен документ». Короткий voice
                # становится текстом хода: поиск идёт по сказанному, ответ приходит
                # сразу. Файл по-прежнему inbox-first; второй раз транскрипт не
                # ингестится (synthetic_document_notice). Длиннее трёх минут — это
                # диктовка, не вопрос: ей достаточно прежнего пути.
                transcript = str((file_ingestion or {}).get("transcript_text") or "").strip()
                try:
                    voice_duration = float(document.get("duration") or 0.0)
                except (TypeError, ValueError):
                    # Неразборчивая длительность от стороннего клиента — не повод
                    # ронять запрос; считаем голос длинным и идём прежним путём.
                    voice_duration = float("inf")
                spoken_question = bool(
                    transcript
                    and str(document.get("media_kind") or "") == "voice"
                    and voice_duration <= _VOICE_QUESTION_MAX_SEC
                )
                if not message:
                    # Два РАЗНЫХ факта, которые до сих пор нёс один флаг:
                    #  * «этот текст сочинил backend» — тогда его нельзя судить
                    #    классификатором как вопрос человека;
                    #  * «файл уже принят отдельно» — тогда его нельзя ингестить
                    #    вторым заходом.
                    # Для транскрипта голоса верен только второй: сказанное вслух —
                    # это слова человека, и вопрос «кто с кем работал», заданный
                    # голосом, обязан получать то же графовое расширение, что
                    # набранный руками. Пока флаг был один, голосовой вопрос
                    # объявлялся системным уведомлением и терял графовый путь.
                    message = transcript[:2000] if spoken_question else f"Загружен документ: {filename}"
                    file_already_ingested = True
                    synthetic_document_notice = not spoken_question
                elif spoken_question:
                    # Голос с подписью: подпись — вопрос человека, транскрипт — материал
                    # текущего хода.
                    attachments.append(
                        {
                            "filename": filename,
                            "transient": True,
                            "transient_text": transcript[:4000],
                            "extraction_success": True,
                        }
                    )

            conversation_id = str(body.get("conversation_id") or "").strip() or None
            channel_chat_id = getattr(request.state, "bridge_chat_id", None)
            if actor.source == "telegram-bridge" and channel_chat_id:
                session = state.storage.get_channel_session(
                    actor.user_id,
                    "telegram",
                    str(channel_chat_id),
                )
                if session and not session.get("is_archived"):
                    conversation_id = str(session["conversation_id"])
                    if requested_mode is None:
                        requested_mode = normalize_conversation_mode(str(session.get("mode") or "dialogue"))

            ingestion_result = None
            if state.auth_service.authorize(actor, "knowledge.create").allowed:
                if file_already_ingested:
                    # The uploaded file already has its own Raw Object and Knowledge Object. The
                    # generated chat text exists only so the agent can acknowledge the upload.
                    ingestion_result = {
                        "promoted": False,
                        "queued_for_review": False,
                        "action": "transient",
                        "category": "system_notice",
                        "reason": "synthetic document acknowledgement; file ingestion handled separately",
                        "synthetic": True,
                    }
                else:
                    ingestion_result = await state.ingestion.ingest_text(
                        actor.user_id,
                        message,
                        source="telegram" if actor.source == "telegram-bridge" else "api",
                        source_ref=source_ref,
                        force_knowledge=force_knowledge,
                        metadata={
                            "channel": actor.source,
                            "chat_id": channel_chat_id,
                            "telegram_message_id": body.get("telegram_message_id"),
                            **({"forward": forward_meta} if forward_meta else {}),
                        },
                    )

            result = await state.agent.chat(
                actor.user_id,
                message,
                actor=actor,
                conversation_id=conversation_id,
                attachments=attachments,
                enable_tools=enable_tools,
                kg=state.kg,
                hybrid_searcher=state.hybrid_searcher,
                ingestion_result=ingestion_result,
                synthetic_document_notice=synthetic_document_notice,
                mode=requested_mode,
            )
            if actor.source == "telegram-bridge" and channel_chat_id:
                state.storage.set_channel_conversation(
                    actor.user_id,
                    "telegram",
                    str(channel_chat_id),
                    result["conversation_id"],
                    mode=str(result.get("context", {}).get("interaction_mode") or "dialogue"),
                )
            result["ingestion"] = ingestion_result
            if file_ingestion:
                result["file_ingestion"] = file_ingestion
            if source_ref and not state.storage.idempotency_complete(
                actor.user_id, source_ref, lease_token, result
            ):
                raise RuntimeError("Lost idempotency lease before response commit")
            return result
        except BaseException:
            if source_ref and lease_token:
                state.storage.idempotency_release(actor.user_id, source_ref, lease_token)
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _queue_assistant_candidate(
        request: Request,
        *,
        required_mode: str | None = None,
    ) -> dict[str, Any]:
        actor = _require(request, "knowledge.create")
        body = await _request_json(request)
        message_id = str(body.get("message_id") or "").strip()
        if not message_id:
            raise HTTPException(status_code=400, detail="message_id is required")
        message_row = request.app.state.storage.get_message(message_id, actor.user_id)
        if not message_row or message_row.get("role") != "assistant":
            raise HTTPException(status_code=404, detail="Assistant message not found")
        metadata = _json_load(message_row.get("metadata_json"), {})
        interaction_mode = str(metadata.get("interaction_mode") or "")
        if interaction_mode not in {"knowledge_work", "research"}:
            raise HTTPException(
                status_code=409,
                detail="Only knowledge_work or research answers can be queued",
            )
        if required_mode and interaction_mode != required_mode:
            raise HTTPException(
                status_code=409,
                detail=f"Only {required_mode}-mode answers can be queued here",
            )
        queue_method = (
            request.app.state.ingestion.queue_research_candidate
            if interaction_mode == "research"
            else request.app.state.ingestion.queue_knowledge_work_candidate
        )
        result = await queue_method(
            actor.user_id,
            str(message_row.get("content") or ""),
            source_ref=f"{interaction_mode}-answer:{message_id}",
            metadata={
                "assistant_message_id": message_id,
                "conversation_id": message_row.get("conversation_id"),
                "requested_by": actor.user_id,
                "interaction_mode": interaction_mode,
                "tools_used": metadata.get("tools_used", []),
                "knowledge_object_ids": metadata.get("knowledge_object_ids", []),
                "knowledge_citations": metadata.get("knowledge_citations", {}),
            },
        )
        return result

    @application.post("/api/assistant/candidates", tags=["knowledge"])
    async def queue_assistant_candidate(request: Request) -> dict[str, Any]:
        return await _queue_assistant_candidate(request)

    @application.post("/api/research/candidates", tags=["knowledge"])
    async def queue_research_candidate(request: Request) -> dict[str, Any]:
        # Kept for bridge/API compatibility; the generalized endpoint also
        # supports knowledge_work results.
        return await _queue_assistant_candidate(request, required_mode="research")

    @application.post("/api/feedback", tags=["feedback"])
    async def feedback(request: Request) -> dict[str, Any]:
        actor = _require(request, "feedback.write")
        body = await _request_json(request)
        try:
            feedback_type = FeedbackType(str(body.get("feedback_type") or "general"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid feedback type") from exc
        score = _parse_json_float(
            body.get("score"),
            field="score",
            default=0.0,
            minimum=-1.0,
            maximum=1.0,
        )
        target_id = str(body.get("target_id") or "").strip()
        if not target_id:
            raise HTTPException(status_code=400, detail="target_id is required")
        try:
            item = await request.app.state.agent.record_feedback(
                actor.user_id,
                str(body.get("target_type") or "answer"),
                target_id,
                feedback_type,
                score,
                str(body.get("comment") or ""),
                body.get("context") if isinstance(body.get("context"), dict) else {},
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"feedback": item}

    # Declared before /api/knowledge/{knowledge_id} so "tags" is never
    # captured as an object id by the path parameter.

    @application.get("/api/search", tags=["retrieval"])
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=2000),
        limit: int = Query(20, ge=1, le=100),
        explain: bool = Query(False),
    ) -> dict[str, Any]:
        """Search one's own knowledge. `explain=true` adds the ranking trace.

        The trace names every candidate that was considered and, for the ones that
        did not make it, WHY — `identifier_mismatch`, `insufficient_evidence`,
        `deprecated_weak`. Those reasons were computed on every query and discarded
        unless an admin asked for them, so "the note is definitely there and search
        says it is not" had no answer short of the admin panel. It is the caller's
        own tenant data, so `search.use` is the whole gate.
        """
        actor = _require(request, "search.use")
        return await request.app.state.hybrid_searcher.search(
            actor.user_id,
            q,
            limit=limit,
            kg=request.app.state.kg,
            explain=explain,
        )

    application.include_router(missions_router)
    application.include_router(missions_admin_router)
    application.include_router(kg_router)
    application.include_router(knowledge_router)
    application.include_router(inbox_router)
    application.include_router(ingest_router)
    application.include_router(conversations_router)
    application.include_router(files_router)
    application.include_router(notifications_router)
    application.include_router(events_router)
    application.include_router(admin_router)
    # Organ-contributed routers (JOP). Built once per process at import time so
    # the app has a stable route set; the registry is authoritative.
    for organ_router in build_registry(settings).routers():
        application.include_router(organ_router)

    @application.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema(request: Request) -> JSONResponse:
        _require(request, "admin.diagnostics")
        return JSONResponse(application.openapi())

    @application.get("/api/docs", include_in_schema=False)
    async def api_docs(request: Request) -> Any:
        _require(request, "admin.diagnostics")
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Friday API")

    static_dir = Path(__file__).parent / "admin_ui" / "static"
    if static_dir.is_dir():
        application.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin")
    return application


app = create_app()


def run_server() -> None:
    import uvicorn

    from friday.telemetry.logging import install_access_log_privacy

    # Access-log lines must not carry query strings (search queries and browse
    # filters are personal data); the filter keeps method/path/status intact.
    install_access_log_privacy()
    runtime_settings = load_settings()
    problems = validate_settings(runtime_settings, production=not runtime_settings.is_loopback_bind)
    errors = [item for item in problems if not item.startswith("warning:")]
    for problem in problems:
        LOGGER.warning("Configuration: %s", problem)
    if errors:
        raise SystemExit("Invalid Friday configuration: " + "; ".join(errors))
    uvicorn.run(
        "friday.server:app",
        host=runtime_settings.api_host,
        port=runtime_settings.api_port,
        reload=False,
        # Friday validates proxy chains itself so it can distinguish the
        # immediate TCP peer from attacker-controlled forwarded values. Letting
        # Uvicorn rewrite ``scope['client']`` first would destroy that evidence
        # and could accidentally turn a forwarded loopback address into the
        # local owner bypass when loopback tokens are disabled.
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=True,
        # TLS, когда владелец положил пару файлов: без неё owner-токен и вся
        # личная база ходят через проброшенный порт открытым текстом.
        ssl_certfile=runtime_settings.ssl_certfile or None,
        ssl_keyfile=runtime_settings.ssl_keyfile or None,
    )
