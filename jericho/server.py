"""FastAPI application for Jericho's local-first knowledge runtime."""

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
import math
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jericho import __version__
from jericho.admin_api import router as admin_router
from jericho.agent_runtime import AgentRuntime
from jericho.agent_runtime.llm import LLMRouter
from jericho.config import (
    JerichoSettings,
    ensure_runtime_dirs,
    load_settings,
    validate_settings,
)
from jericho.diagnostics.runtime_lease import ProcessLease
from jericho.execution_kernel import ExecutionKernel
from jericho.executive import ExecutiveService
from jericho.executive.api import admin_router as missions_admin_router
from jericho.executive.api import router as missions_router
from jericho.ingestion import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IngestionPipeline,
)
from jericho.knowledge_graph import KnowledgeGraph
from jericho.memory import MemoryVault
from jericho.organs import ServiceContext, build_registry
from jericho.permissions import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthenticationError,
    AuthorizationError,
    AuthorizationService,
    bind_actor,
)
from jericho.retrieval import EmbeddingBackend, HybridSearcher
from jericho.security import verify_bridge_request
from jericho.storage import init_storage, normalize_conversation_mode
from jericho.storage.models import (
    AuditEntry,
    EntityType,
    FeedbackType,
    InboxStatus,
    RelationType,
    ResolutionStatus,
    new_id,
)
from jericho.web_surfer import WebSurfer
from jericho.workers import IntervalTask, WorkersManager

LOGGER = logging.getLogger(__name__)
VERSION = __version__
_REALM_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


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


def _max_request_body_bytes(settings: JerichoSettings) -> int:
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


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _client_ip(request: Request, settings: JerichoSettings) -> str:
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
    return str(chain[0])


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.casefold() in {"localhost", "testclient"}


# Hostnames a local browser legitimately uses to reach a loopback-bound API.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# Methods that cannot mutate state; cross-origin reads stay blocked by CORS.
_BROWSER_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _request_hostname(value: str) -> str | None:
    """Lowercase hostname from a ``Host`` header or an ``Origin`` URL."""
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return urlsplit(candidate if "//" in candidate else f"//{candidate}").hostname
    except ValueError:
        return None


def _guard_loopback_browser_request(request: Request, settings: JerichoSettings) -> None:
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
    if request.method in _BROWSER_SAFE_METHODS:
        return
    origin = request.headers.get("origin", "").strip()
    if origin:
        if origin in settings.cors_origins or _request_hostname(origin) in _LOOPBACK_HOSTNAMES:
            return
        raise AuthorizationError("Cross-origin browser requests cannot use loopback authentication")
    fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise AuthorizationError("Cross-site browser requests cannot use loopback authentication")


def _telegram_user_id(settings: JerichoSettings, external_id: str) -> str:
    realm = _REALM_RE.sub("_", settings.telegram_realm_id).strip("._-") or "telegram"
    return f"telegram:{realm}:{external_id}"


async def _request_json(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "json_body", None)
    if isinstance(cached, dict):
        return cached
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    request.state.json_body = body
    return body


def _parse_json_bool(value: Any, *, field: str, default: bool) -> bool:
    """Accept only real JSON booleans for externally visible control flags."""

    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a boolean")
    return value


def _parse_json_float(
    value: Any,
    *,
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a finite bounded number and reject booleans/NaN/infinity."""

    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc
    if not math.isfinite(parsed):
        raise HTTPException(status_code=400, detail=f"{field} must be finite")
    if not minimum <= parsed <= maximum:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be between {minimum:g} and {maximum:g}",
        )
    return parsed


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


def _require(request: Request, capability: str) -> ActorContext:
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


def _audit(
    request: Request,
    action: str,
    target_type: str,
    target_id: str | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    request.app.state.storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id=request.state.actor.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip_address=getattr(request.state, "client_ip", ""),
            request_id=getattr(request.state, "request_id", ""),
        )
    )


def _safe_owned_file(root: Path, candidate: str) -> Path:
    root = root.resolve()
    path = Path(candidate).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="File not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


async def _authenticate(request: Request) -> ActorContext:
    state = request.app.state
    settings: JerichoSettings = state.settings
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.casefold().startswith("bearer ") else ""
    signature = request.headers.get("x-jericho-signature", "")

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
            timestamp=request.headers.get("x-jericho-timestamp", ""),
            method=request.method,
            path=signed_path,
            external_user_id=request.headers.get("x-jericho-user", ""),
            chat_id=request.headers.get("x-jericho-chat", ""),
            nonce=request.headers.get("x-jericho-nonce", ""),
            body=raw_body,
            signature=signature,
            max_age_sec=settings.telegram_signature_max_age_sec,
        )
        chat_number = int(identity.chat_id)
        # Deny-by-default: only chats on the effective allowlist (allowlist plus
        # owner chats) may authenticate. An empty allowlist denies every chat.
        # Raise AuthorizationError (403) so the bridge dead-letters the update
        # instead of retrying an unauthorized chat hundreds of times.
        if chat_number not in settings.telegram_effective_allowed_chat_ids:
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
        user_id = _telegram_user_id(settings, identity.external_user_id)
        existing = state.storage.get_user(user_id)
        state.storage.ensure_user(
            user_id,
            source="telegram",
            external_id=identity.external_user_id,
            display_name=display_name,
            username=str(telegram_user.get("username") or ""),
            preset_key="user",
            metadata={"chat_id": identity.chat_id, "language_code": telegram_user.get("language_code")},
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
        if settings.api_token and hmac.compare_digest(bearer, settings.api_token):
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

    client_ip = _client_ip(request, settings)
    if not settings.api_require_token_on_loopback and _is_loopback(client_ip):
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
    settings: JerichoSettings = request.app.state.settings
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


def create_app(settings_override: JerichoSettings | None = None) -> FastAPI:
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
            protocol="jericho.backend.v1",
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
            searcher = HybridSearcher(storage, embeddings, graph_max_depth=settings.graph_max_depth)
            graph = KnowledgeGraph(storage)
            ingestion = IngestionPipeline(settings, storage, graph, llm)
            web_surfer = WebSurfer(settings)
            kernel = ExecutionKernel(auth_service, settings)
            kernel.bind_services(storage, graph, web_surfer, ingestion)
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
                settings=settings, storage=storage, kg=graph, ingestion=ingestion, llm=llm
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
            LOGGER.info("Jericho API started on %s:%s", settings.api_host, settings.api_port)
            try:
                yield
            finally:
                await workers.stop()
                await web_surfer.close()
                storage.close()
                LOGGER.info("Jericho API stopped")

    application = FastAPI(
        title="Jericho API",
        version=VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
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
            "X-Jericho-Timestamp",
            "X-Jericho-User",
            "X-Jericho-Chat",
            "X-Jericho-Signature",
            "X-Request-ID",
        ],
    )

    @application.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        path = request.url.path
        request.state.request_id = request.headers.get("x-request-id") or secrets.token_hex(12)
        request.state.client_ip = _client_ip(request, settings)
        public = path == "/" or path == "/api/health" or path == "/admin" or path.startswith("/admin/")
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

            try:
                if await limiter.exhausted(failure_key, failure_limit):
                    raise HTTPException(
                        status_code=429,
                        detail="Too many failed authentication attempts",
                        headers={"Retry-After": "60"},
                    )
                actor = await _authenticate(request)
                await _enforce_rate_limit(request, actor)
                request.state.actor = actor
                with bind_actor(actor):
                    response = await call_next(request)
            except AuthenticationError as exc:
                await _count_auth_failure()
                response = JSONResponse({"detail": str(exc)}, status_code=401)
            except AuthorizationError as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=403)
            except HTTPException as exc:
                # Exceptions raised before ``call_next`` are outside FastAPI's
                # route exception handlers.  Convert them here so middleware
                # checks such as rate limiting cannot accidentally surface as
                # an internal server error.
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            except ValueError as exc:
                # Malformed credentials (bad timestamps, chat ids, …) are
                # authentication failures too and consume the same budget.
                await _count_auth_failure()
                response = JSONResponse({"detail": str(exc)}, status_code=401)
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
        synthetic_document_notice = False
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
                    transient_file = state.ingestion.inspect_file_transient(
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
                if not message:
                    message = f"Загружен документ: {filename}"
                    synthetic_document_notice = True

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
                if synthetic_document_notice:
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

    @application.post("/api/conversations/channel/reset", tags=["chat"])
    async def reset_channel_conversation(request: Request) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        body = await _request_json(request)
        channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
        channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
        if not channel_id:
            raise HTTPException(status_code=400, detail="channel_id is required")
        cleared = request.app.state.storage.clear_channel_conversation(actor.user_id, channel, channel_id)
        return {"status": "reset", "cleared": cleared}

    @application.post("/api/conversations/channel/mode", tags=["chat"])
    async def set_channel_mode(request: Request) -> dict[str, Any]:
        actor = _require(request, "chat.use")
        body = await _request_json(request)
        channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
        channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
        if not channel_id:
            raise HTTPException(status_code=400, detail="channel_id is required")
        try:
            mode = normalize_conversation_mode(str(body.get("mode") or "dialogue"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = request.app.state.storage.get_channel_session(actor.user_id, channel, channel_id)
        if session:
            updated = request.app.state.storage.set_channel_mode(
                actor.user_id,
                channel,
                channel_id,
                mode,
            )
            return {"mode": mode, "session": updated, "conversation_created": False}
        conversation = request.app.state.storage.create_conversation(
            actor.user_id,
            title=f"{channel} {mode}",
            mode=mode,
        )
        request.app.state.storage.set_channel_conversation(
            actor.user_id,
            channel,
            channel_id,
            str(conversation["id"]),
            mode=mode,
        )
        return {
            "mode": mode,
            "session": request.app.state.storage.get_channel_session(actor.user_id, channel, channel_id),
            "conversation_created": True,
        }

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

    @application.post("/api/ingest", tags=["knowledge"])
    async def ingest(request: Request) -> dict[str, Any]:
        actor = _require(request, "knowledge.create")
        body = await _request_json(request)
        content = str(body.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content is required")
        force_knowledge = _parse_json_bool(
            body.get("force_knowledge"), field="force_knowledge", default=False
        )
        return await request.app.state.ingestion.ingest_text(
            actor.user_id,
            content,
            source="api",
            source_ref=str(body.get("source_ref") or ""),
            force_knowledge=force_knowledge,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )

    @application.post("/api/ingest/url", tags=["knowledge"])
    async def ingest_url(request: Request) -> dict[str, Any]:
        # Fetching the public web needs web.fetch; turning it into knowledge
        # needs knowledge.create. Both are enforced before anything happens.
        actor = _require(request, "web.fetch")
        request.app.state.auth_service.require(actor, "knowledge.create")
        body = await _request_json(request)
        url = str(body.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        result = await request.app.state.web_surfer.fetch(url)
        if result.error or not result.text.strip():
            # fetch() never raises — SSRF blocks, non-2xx and empty pages all
            # surface as an error string; ingesting empty text is refused.
            raise HTTPException(
                status_code=422,
                detail=f"Could not fetch a readable page: {result.error or 'empty content'}",
            )
        title = result.title or result.url
        # The page is captured as a Raw Object and routed through the Inbox like
        # any other material — it becomes a retrievable Knowledge Object only
        # after review, never silently.
        outcome = await request.app.state.ingestion.ingest_text(
            actor.user_id,
            result.text,
            source="web",
            source_ref=result.url,
            metadata={
                "url": result.url,
                "title": title,
                "status_code": result.status_code,
                "content_source": "web_fetch",
            },
        )
        _audit(
            request,
            "knowledge.ingest_url",
            "raw_object",
            outcome.get("raw_object_id"),
            after={"url": result.url},
        )
        return {**outcome, "url": result.url, "title": title}

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

    def _require_bridge(request: Request) -> ActorContext:
        # The outbound queue is drained only by the Telegram bridge — the sole
        # holder of the bridge secret. Bearer/loopback actors are refused.
        actor = request.state.actor
        if actor.source != "telegram-bridge":
            raise HTTPException(status_code=403, detail="Bridge authentication required")
        return actor

    @application.get("/api/notifications/pending", tags=["notifications"])
    async def notifications_pending(
        request: Request,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        _require_bridge(request)
        settings: JerichoSettings = request.app.state.settings
        allowed = settings.telegram_effective_allowed_chat_ids
        items = []
        for row in request.app.state.storage.list_pending_notifications(limit=limit):
            # Defence in depth: never hand the bridge a de-allowlisted chat.
            try:
                if int(str(row.get("chat_id"))) not in allowed:
                    continue
            except (TypeError, ValueError):
                continue
            items.append({"id": row["id"], "chat_id": row["chat_id"], "body": row["body"]})
        return {"items": items, "count": len(items)}

    @application.post("/api/notifications/ack", tags=["notifications"])
    async def notifications_ack(request: Request) -> dict[str, Any]:
        _require_bridge(request)
        body = await _request_json(request)
        raw_sent = body.get("sent")
        raw_failed = body.get("failed")
        sent = [str(x) for x in raw_sent] if isinstance(raw_sent, list) else []
        failed = [str(x) for x in raw_failed] if isinstance(raw_failed, list) else []
        request.app.state.storage.mark_notifications(sent, failed)
        return {"sent": len(sent), "failed": len(failed)}

    @application.get("/api/knowledge", tags=["knowledge"])
    async def list_knowledge(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        actor = _require(request, "knowledge.read")
        items = request.app.state.storage.list_knowledge_objects(
            actor.user_id,
            limit=limit,
            offset=offset,
            lifecycle_stage=lifecycle_stage,
            tag=tag,
            entity_id=entity_id,
        )
        return {"items": items, "count": len(items)}

    # Declared before /api/knowledge/{knowledge_id} so "tags" is never
    # captured as an object id by the path parameter.
    @application.get("/api/knowledge/tags", tags=["knowledge"])
    async def list_knowledge_tags(
        request: Request,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = _require(request, "knowledge.read")
        items = request.app.state.storage.list_knowledge_tags(actor.user_id, limit=limit)
        return {"items": items, "count": len(items)}

    @application.get("/api/knowledge/{knowledge_id}", tags=["knowledge"])
    async def get_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "knowledge.read")
        item = request.app.state.storage.get_knowledge_object(knowledge_id, actor.user_id)
        if not item:
            raise HTTPException(status_code=404, detail="Knowledge object not found")
        return {
            "item": item,
            "versions": request.app.state.storage.list_knowledge_versions(knowledge_id, actor.user_id),
            "entity_links": request.app.state.storage.list_knowledge_entity_links(
                actor.user_id,
                knowledge_object_id=knowledge_id,
                status=None,
            ),
        }

    @application.patch("/api/knowledge/{knowledge_id}", tags=["knowledge"])
    async def update_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "knowledge.edit")
        body = await _request_json(request)
        state = request.app.state
        before = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
        if not before:
            raise HTTPException(status_code=404, detail="Knowledge object not found")
        allowed = {
            "title",
            "summary",
            "content",
            "tags_json",
            "importance",
            "lifecycle_stage",
            "knowledge_kind",
            "quality_score",
        }
        updates = {key: body[key] for key in allowed if key in body}
        try:
            after = state.storage.update_knowledge_fields(knowledge_id, actor.user_id, **updates)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "knowledge.update", "knowledge_object", knowledge_id, before=before, after=after)
        return {"item": after}

    @application.delete("/api/knowledge/{knowledge_id}", tags=["knowledge"])
    async def delete_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "knowledge.delete")
        state = request.app.state
        before = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
        if not before or not state.storage.soft_delete_knowledge_object(knowledge_id, actor.user_id):
            raise HTTPException(status_code=404, detail="Knowledge object not found")
        after = state.storage.get_knowledge_object(knowledge_id, actor.user_id)
        _audit(request, "knowledge.delete", "knowledge_object", knowledge_id, before=before, after=after)
        return {"status": "soft_deleted"}

    @application.get("/api/inbox", tags=["inbox"])
    async def list_inbox(
        request: Request,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        actor = _require(request, "inbox.read")
        try:
            status_enum = InboxStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid inbox status") from exc
        items = request.app.state.storage.list_inbox(
            actor.user_id,
            status_enum,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "count": len(items)}

    @application.post("/api/inbox/{inbox_id}/classify", tags=["inbox"])
    async def classify_inbox(inbox_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "inbox.review")
        body = await _request_json(request)
        try:
            status = InboxStatus(str(body.get("status") or "classified"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid inbox status") from exc
        item = request.app.state.ingestion.classify_inbox_item(
            actor.user_id,
            inbox_id,
            status,
            entity_id=body.get("entity_id"),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
            notes=str(body.get("notes") or ""),
            reviewed_by=actor.user_id,
        )
        if not item:
            raise HTTPException(status_code=404, detail="Inbox item not found")
        _audit(request, "inbox.classify", "inbox", inbox_id, after=item)
        return {"item": item}

    @application.get("/api/kg/stats", tags=["knowledge-graph"])
    async def graph_stats(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        state = request.app.state
        stats = state.kg.get_stats(actor.user_id)
        channel_id = str(request.headers.get("x-jericho-chat") or "").strip()
        if channel_id:
            session = state.storage.get_channel_session(
                actor.user_id,
                "telegram",
                channel_id,
            )
            stats["interaction_mode"] = str((session or {}).get("mode") or "dialogue")
        if state.kg.is_empty(actor.user_id):
            stats["bootstrap_suggestions"] = state.kg.get_bootstrap_suggestions(actor.user_id)
        return stats

    @application.get("/api/kg/entities", tags=["knowledge-graph"])
    async def list_entities(
        request: Request,
        entity_type: str | None = None,
        q: str | None = None,
        limit: int = Query(100, ge=1, le=5000),
    ) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        try:
            parsed_type = EntityType(entity_type) if entity_type else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid entity type") from exc
        state = request.app.state
        if q and q.strip():
            # Name lookup for browse surfaces: exact/alias/token matches only.
            matches = state.kg.search_entities(actor.user_id, q.strip(), limit=min(limit, 25))
            if parsed_type:
                matches = [item for item in matches if item.get("entity_type") == parsed_type.value]
            return {"items": matches, "count": len(matches)}
        items = state.kg.list_entities(actor.user_id, parsed_type, limit=limit)
        return {"items": items, "count": len(items)}

    @application.post("/api/kg/entities", tags=["knowledge-graph"])
    async def create_entity(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        try:
            entity_type = EntityType(str(body.get("entity_type") or "other"))
            entity = request.app.state.kg.create_entity(
                actor.user_id,
                str(body.get("name") or ""),
                entity_type,
                aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else [],
                description=str(body.get("description") or ""),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "entity.create", "entity", entity.get("id"), after=entity)
        return {"entity": entity}

    @application.get("/api/kg/containers", tags=["knowledge-graph"])
    async def list_containers(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        items = request.app.state.kg.list_containers(actor.user_id)
        return {"items": items, "count": len(items)}

    @application.post("/api/kg/containers", tags=["knowledge-graph"])
    async def create_container(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        try:
            container = request.app.state.kg.create_container(
                actor.user_id,
                str(body.get("name") or ""),
                kind=str(body.get("kind") or EntityType.COLLECTION.value),
                parent_id=str(body.get("parent_id") or "") or None,
                description=str(body.get("description") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "container.create", "entity", container.get("id"), after=container)
        return {"container": container}

    @application.get("/api/kg/entities/{entity_id}", tags=["knowledge-graph"])
    async def get_entity(entity_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        entity = request.app.state.kg.get_entity(entity_id, actor.user_id)
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        return {
            "entity": entity,
            "relations": request.app.state.kg.get_entity_relations(entity_id, actor.user_id),
            "knowledge": request.app.state.kg.get_entity_knowledge(entity_id, actor.user_id),
            "versions": request.app.state.storage.list_entity_versions(entity_id, actor.user_id),
        }

    @application.patch("/api/kg/entities/{entity_id}", tags=["knowledge-graph"])
    async def update_entity(entity_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        state = request.app.state
        before = state.kg.get_entity(entity_id, actor.user_id)
        if not before:
            raise HTTPException(status_code=404, detail="Entity not found")
        try:
            after = state.kg.update_entity(actor.user_id, entity_id, **body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "entity.update", "entity", entity_id, before=before, after=after)
        return {"entity": after}

    @application.delete("/api/kg/entities/{entity_id}", tags=["knowledge-graph"])
    async def delete_entity(entity_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        state = request.app.state
        before = state.kg.get_entity(entity_id, actor.user_id)
        if not before or not state.kg.delete_entity(actor.user_id, entity_id):
            raise HTTPException(status_code=404, detail="Entity not found")
        _audit(request, "entity.delete", "entity", entity_id, before=before)
        return {"status": "soft_deleted"}

    @application.post("/api/kg/relations", tags=["knowledge-graph"])
    async def create_relation(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        try:
            raw_metadata = body.get("metadata")
            user_metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            relation = request.app.state.kg.create_relation(
                actor.user_id,
                str(body.get("source_entity_id") or ""),
                str(body.get("target_entity_id") or ""),
                RelationType(str(body.get("relation_type") or "related_to")),
                weight=_parse_json_float(
                    body.get("weight"),
                    field="weight",
                    default=1.0,
                    minimum=0.0,
                    maximum=1.0,
                ),
                # Reserved provenance keys are stamped after user metadata so a
                # request body cannot forge who created the edge or how.
                metadata={**user_metadata, "created_by": actor.user_id},
                origin="api",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "relation.create", "relation", relation.id, after=relation.to_row())
        return {"relation": relation.to_row()}

    @application.post("/api/kg/link", tags=["knowledge-graph"])
    async def link_knowledge(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        try:
            link = request.app.state.kg.link_knowledge_to_entity(
                str(body.get("knowledge_object_id") or ""),
                str(body.get("entity_id") or ""),
                actor.user_id,
                confidence=_parse_json_float(
                    body.get("confidence"),
                    field="confidence",
                    default=1.0,
                    minimum=0.0,
                    maximum=1.0,
                ),
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                status=str(body.get("status") or "accepted"),
                reviewed_by=actor.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "knowledge.entity_link", "knowledge_entity_link", link.get("id"), after=link)
        return {"link": link}

    @application.get("/api/kg/graph/{entity_id}", tags=["knowledge-graph"])
    async def entity_graph(
        entity_id: str,
        request: Request,
        depth: int = Query(2, ge=0, le=5),
    ) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        graph = request.app.state.kg.get_entity_graph(actor.user_id, entity_id, depth)
        if not graph.get("nodes"):
            raise HTTPException(status_code=404, detail="Entity not found")
        return graph

    @application.post("/api/kg/resolutions/detect", tags=["knowledge-graph"])
    async def detect_duplicates(request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        candidates = request.app.state.kg.resolver.detect_duplicates(actor.user_id, min_confidence=0.55)
        return {"items": [candidate.to_row() for candidate in candidates], "count": len(candidates)}

    @application.get("/api/kg/resolutions", tags=["knowledge-graph"])
    async def list_resolutions(request: Request, status: str | None = None) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        try:
            parsed = ResolutionStatus(status) if status else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid resolution status") from exc
        items = request.app.state.storage.list_resolution_candidates(actor.user_id, parsed)
        return {"items": items, "count": len(items)}

    @application.get("/api/kg/resolutions/pending", tags=["knowledge-graph"])
    async def pending_resolutions(request: Request) -> dict[str, Any]:
        # Enriched with both entities' names and link/relation counts so review
        # surfaces (e.g. the Telegram /merges flow) can show a human-readable pair.
        actor = _require(request, "kg.read")
        items = request.app.state.kg.resolver.get_pending_resolutions(actor.user_id)
        return {"items": items, "count": len(items)}

    @application.post("/api/kg/resolutions/{candidate_id}/accept", tags=["knowledge-graph"])
    async def accept_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.merge")
        body = await _request_json(request)
        try:
            merged = request.app.state.kg.resolver.accept_resolution(
                candidate_id,
                actor.user_id,
                target_entity_id=body.get("target_entity_id"),
                resolved_by=actor.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "entity.merge", "resolution", candidate_id, after=merged)
        return {"result": merged}

    @application.post("/api/kg/resolutions/{candidate_id}/reject", tags=["knowledge-graph"])
    async def reject_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.merge")
        try:
            request.app.state.kg.resolver.reject_resolution(
                candidate_id,
                actor.user_id,
                resolved_by=actor.user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit(request, "entity.merge_rejected", "resolution", candidate_id)
        return {"status": "rejected"}

    @application.get("/api/kg/timeline", tags=["knowledge-graph"])
    async def event_timeline(
        request: Request,
        start: str | None = None,
        end: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> dict[str, Any]:
        actor = _require(request, "kg.read")
        try:
            events = request.app.state.kg.timeline(actor.user_id, start=start, end=end, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"items": events, "count": len(events), "start": start, "end": end}

    @application.post("/api/kg/entities/{entity_id}/time", tags=["knowledge-graph"])
    async def set_event_time(entity_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "kg.write")
        body = await _request_json(request)
        occurred_at = str(body.get("occurred_at") or "").strip()
        if not occurred_at:
            raise HTTPException(status_code=400, detail="occurred_at is required")
        occurred_end_value = body.get("occurred_end")
        occurred_end = str(occurred_end_value).strip() if occurred_end_value else None
        precision_value = body.get("precision")
        precision = str(precision_value).strip() if precision_value else None
        try:
            record = request.app.state.kg.set_event_time(
                actor.user_id,
                entity_id,
                occurred_at,
                occurred_end=occurred_end,
                precision=precision,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, "entity.time_set", "entity", entity_id, after=record)
        return {"event_time": record}

    @application.get("/api/search", tags=["retrieval"])
    async def search(
        request: Request,
        q: str = Query(min_length=1, max_length=2000),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        actor = _require(request, "search.use")
        return await request.app.state.hybrid_searcher.search(
            actor.user_id,
            q,
            limit=limit,
            kg=request.app.state.kg,
        )

    @application.post("/api/files", tags=["files"])
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        source_ref: str = Form(""),
    ) -> dict[str, Any]:
        actor = _require(request, "files.upload")
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="File is too large")
        result = await request.app.state.ingestion.ingest_file(
            actor.user_id,
            None,
            content,
            filename=file.filename or "upload.bin",
            mime_type=file.content_type or "application/octet-stream",
            source_ref=source_ref,
            metadata={"uploaded_via": "api"},
        )
        _audit(
            request,
            "file.upload",
            "raw_object",
            result.get("raw_object_id"),
            after={"filename": file.filename, "size_bytes": len(content)},
        )
        return result

    @application.get("/api/files", tags=["files"])
    async def list_files(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = _require(request, "files.read")
        rows = request.app.state.storage.execute(
            """SELECT id, source_ref, metadata_json, received_at, deleted_at
               FROM raw_objects
               WHERE user_id=? AND content_type='file' AND deleted_at IS NULL
               ORDER BY received_at DESC LIMIT ?""",
            (actor.user_id, limit),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["metadata"] = _json_load(item.pop("metadata_json", "{}"), {})
            items.append(item)
        return {"items": items, "count": len(items)}

    @application.get("/api/files/{raw_id}", tags=["files"])
    async def download_file(raw_id: str, request: Request):
        actor = _require(request, "files.read")
        state = request.app.state
        raw = state.storage.get_raw_object(raw_id, actor.user_id)
        if not raw or raw.get("content_type") != "file" or raw.get("deleted_at"):
            raise HTTPException(status_code=404, detail="File not found")
        metadata = _json_load(raw.get("metadata_json"), {})
        path = _safe_owned_file(state.settings.files_dir, str(metadata.get("stored_path") or ""))
        # File bytes leaving the system are always audited.
        _audit(
            request,
            "file.download",
            "raw_object",
            raw_id,
            after={"filename": str(metadata.get("filename") or "")},
        )
        return FileResponse(path, filename=str(metadata.get("filename") or path.name))

    @application.get("/api/conversations", tags=["chat"])
    async def conversations(
        request: Request,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        actor = _require(request, "conversations.read")
        items = request.app.state.storage.list_conversations(
            actor.user_id,
            include_archived=include_archived,
        )
        return {"items": items, "count": len(items)}

    @application.get("/api/conversations/{conversation_id}/messages", tags=["chat"])
    async def conversation_messages(
        conversation_id: str,
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        actor = _require(request, "conversations.read")
        if not request.app.state.storage.get_conversation(conversation_id, actor.user_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        items = request.app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=actor.user_id,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    @application.post("/api/conversations/{conversation_id}/archive", tags=["chat"])
    async def archive_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "conversations.manage")
        body = await _request_json(request)
        archived = _parse_json_bool(body.get("archived"), field="archived", default=True)
        updated = request.app.state.storage.set_conversation_archived(
            conversation_id, actor.user_id, archived
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"conversation": updated}

    @application.delete("/api/conversations/{conversation_id}", tags=["chat"])
    async def delete_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
        actor = _require(request, "conversations.manage")
        report = request.app.state.storage.delete_conversation(conversation_id, actor.user_id)
        if not report.get("existed"):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"status": "deleted", "report": report}

    application.include_router(missions_router)
    application.include_router(missions_admin_router)
    application.include_router(admin_router)
    # Organ-contributed routers (JOP). Built once per process at import time so
    # the app has a stable route set; the registry is authoritative.
    for organ_router in build_registry(settings).routers():
        application.include_router(organ_router)

    static_dir = Path(__file__).parent / "admin_ui" / "static"
    if static_dir.is_dir():
        application.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin")
    return application


app = create_app()


def run_server() -> None:
    import uvicorn

    from jericho.telemetry.logging import install_access_log_privacy

    # Access-log lines must not carry query strings (search queries and browse
    # filters are personal data); the filter keeps method/path/status intact.
    install_access_log_privacy()
    runtime_settings = load_settings()
    problems = validate_settings(runtime_settings, production=not runtime_settings.is_loopback_bind)
    errors = [item for item in problems if not item.startswith("warning:")]
    for problem in problems:
        LOGGER.warning("Configuration: %s", problem)
    if errors:
        raise SystemExit("Invalid Jericho configuration: " + "; ".join(errors))
    uvicorn.run(
        "jericho.server:app",
        host=runtime_settings.api_host,
        port=runtime_settings.api_port,
        reload=False,
        # Jericho validates proxy chains itself so it can distinguish the
        # immediate TCP peer from attacker-controlled forwarded values. Letting
        # Uvicorn rewrite ``scope['client']`` first would destroy that evidence
        # and could accidentally turn a forwarded loopback address into the
        # local owner bypass when loopback tokens are disabled.
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=True,
    )
